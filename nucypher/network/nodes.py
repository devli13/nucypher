"""
This file is part of nucypher.

nucypher is free software: you can redistribute it and/or modify
it under the terms of the GNU Affero General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

nucypher is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU Affero General Public License for more details.

You should have received a copy of the GNU Affero General Public License
along with nucypher.  If not, see <https://www.gnu.org/licenses/>.
"""

import binascii
import contextlib
import random
import time
from collections import defaultdict, OrderedDict
from collections import deque
from collections import namedtuple
from contextlib import suppress
from typing import Set, Tuple, Union

import maya
import requests
from eth_utils import to_checksum_address

from bytestring_splitter import BytestringSplitter, PartiallyKwargifiedBytes
from bytestring_splitter import VariableLengthBytestring, BytestringSplittingError
from constant_sorrow import constant_or_bytes
from constant_sorrow.constants import (
    NO_KNOWN_NODES,
    NOT_SIGNED,
    NEVER_SEEN,
    NO_STORAGE_AVAILIBLE,
    FLEET_STATES_MATCH,
    CERTIFICATE_NOT_SAVED,
    UNKNOWN_FLEET_STATE
)
from cryptography.x509 import Certificate
from requests.exceptions import SSLError
from twisted.internet import reactor, defer
from twisted.internet import task
from twisted.internet.threads import deferToThread
from twisted.logger import Logger
from umbral.signing import Signature

from nucypher.blockchain.economics import TokenEconomicsFactory
from nucypher.blockchain.eth.agents import ContractAgency, StakingEscrowAgent
from nucypher.blockchain.eth.interfaces import BlockchainInterface
from nucypher.blockchain.eth.registry import BaseContractRegistry
from nucypher.config.constants import SeednodeMetadata
from nucypher.config.storages import ForgetfulNodeStorage
from nucypher.crypto.api import keccak_digest, verify_eip_191, recover_address_eip_191
from nucypher.crypto.constants import PUBLIC_ADDRESS_LENGTH
from nucypher.crypto.kits import UmbralMessageKit
from nucypher.crypto.powers import TransactingPower, SigningPower, DecryptingPower, NoSigningPower
from nucypher.crypto.signing import signature_splitter
from nucypher.network import LEARNING_LOOP_VERSION
from nucypher.network.exceptions import NodeSeemsToBeDown
from nucypher.network.middleware import RestMiddleware
from nucypher.network.nicknames import nickname_from_seed
from nucypher.network.protocols import SuspiciousActivity
from nucypher.network.server import TLSHostingPower


def icon_from_checksum(checksum,
                       nickname_metadata,
                       number_of_nodes="Unknown number of "):
    if checksum is NO_KNOWN_NODES:
        return "NO FLEET STATE AVAILABLE"
    icon_template = """
    <div class="nucypher-nickname-icon" style="border-color:{color};">
    <div class="small">{number_of_nodes} nodes</div>
    <div class="symbols">
        <span class="single-symbol" style="color: {color}">{symbol}&#xFE0E;</span>
    </div>
    <br/>
    <span class="small-address">{fleet_state_checksum}</span>
    </div>
    """.replace("  ", "").replace('\n', "")
    return icon_template.format(
        number_of_nodes=number_of_nodes,
        color=nickname_metadata[0][0]['hex'],
        symbol=nickname_metadata[0][1],
        fleet_state_checksum=checksum[0:8]
    )


class FleetStateTracker:
    """
    A representation of a fleet of NuCypher nodes.
    """
    _checksum = NO_KNOWN_NODES.bool_value(False)
    _nickname = NO_KNOWN_NODES
    _nickname_metadata = NO_KNOWN_NODES
    _tracking = False
    most_recent_node_change = NO_KNOWN_NODES
    snapshot_splitter = BytestringSplitter(32, 4)
    log = Logger("Learning")
    state_template = namedtuple("FleetState", ("nickname", "metadata", "icon", "nodes", "updated"))

    def __init__(self):
        self.additional_nodes_to_track = []
        self.updated = maya.now()
        self._nodes = OrderedDict()
        self.states = OrderedDict()

    def __setitem__(self, key, value):
        self._nodes[key] = value

        if self._tracking:
            self.log.info("Updating fleet state after saving node {}".format(value))
            self.record_fleet_state()
        else:
            self.log.debug("Not updating fleet state.")

    def __getitem__(self, item):
        return self._nodes[item]

    def __bool__(self):
        return bool(self._nodes)

    def __contains__(self, item):
        return item in self._nodes.keys() or item in self._nodes.values()

    def __iter__(self):
        yield from self._nodes.values()

    def __len__(self):
        return len(self._nodes)

    def __eq__(self, other):
        return self._nodes == other._nodes

    def __repr__(self):
        return self._nodes.__repr__()

    @property
    def checksum(self):
        return self._checksum

    @checksum.setter
    def checksum(self, checksum_value):
        self._checksum = checksum_value
        self._nickname, self._nickname_metadata = nickname_from_seed(checksum_value, number_of_pairs=1)

    @property
    def nickname(self):
        return self._nickname

    @property
    def nickname_metadata(self):
        return self._nickname_metadata

    @property
    def icon(self) -> str:
        if self.nickname_metadata is NO_KNOWN_NODES:
            return str(NO_KNOWN_NODES)
        return self.nickname_metadata[0][1]

    def addresses(self):
        return self._nodes.keys()

    def icon_html(self):
        return icon_from_checksum(checksum=self.checksum,
                                  number_of_nodes=str(len(self)),
                                  nickname_metadata=self.nickname_metadata)

    def snapshot(self):
        fleet_state_checksum_bytes = binascii.unhexlify(self.checksum)
        fleet_state_updated_bytes = self.updated.epoch.to_bytes(4, byteorder="big")
        return fleet_state_checksum_bytes + fleet_state_updated_bytes

    def record_fleet_state(self, additional_nodes_to_track=None):
        if additional_nodes_to_track:
            self.additional_nodes_to_track.extend(additional_nodes_to_track)
        if not self._nodes:
            # No news here.
            return
        sorted_nodes = self.sorted()

        sorted_nodes_joined = b"".join(bytes(n) for n in sorted_nodes)
        checksum = keccak_digest(sorted_nodes_joined).hex()
        if checksum not in self.states:
            self.checksum = keccak_digest(b"".join(bytes(n) for n in self.sorted())).hex()
            self.updated = maya.now()
            # For now we store the sorted node list.  Someday we probably spin this out into
            # its own class, FleetState, and use it as the basis for partial updates.
            new_state = self.state_template(nickname=self.nickname,
                                            metadata=self.nickname_metadata,
                                            nodes=sorted_nodes,
                                            icon=self.icon,
                                            updated=self.updated,
                                            )
            self.states[checksum] = new_state
            return checksum, new_state

    def start_tracking_state(self, additional_nodes_to_track=None):
        if additional_nodes_to_track is None:
            additional_nodes_to_track = list()
        self.additional_nodes_to_track.extend(additional_nodes_to_track)
        self._tracking = True
        self.update_fleet_state()

    def sorted(self):
        nodes_to_consider = list(self._nodes.values()) + self.additional_nodes_to_track
        return sorted(nodes_to_consider, key=lambda n: n.checksum_address)

    def shuffled(self):
        nodes_we_know_about = list(self._nodes.values())
        random.shuffle(nodes_we_know_about)
        return nodes_we_know_about

    def abridged_states_dict(self):
        abridged_states = {}
        for k, v in self.states.items():
            abridged_states[k] = self.abridged_state_details(v)

        return abridged_states

    def abridged_nodes_dict(self):
        abridged_nodes = {}
        for checksum_address, node in self._nodes.items():
            abridged_nodes[checksum_address] = self.abridged_node_details(node)

        return abridged_nodes

    @staticmethod
    def abridged_state_details(state):
        return {"nickname": state.nickname,
                "symbol": state.metadata[0][1],
                "color_hex": state.metadata[0][0]['hex'],
                "color_name": state.metadata[0][0]['color'],
                "updated": state.updated.rfc2822()
                }

    @staticmethod
    def abridged_node_details(node):
        try:
            last_seen = node.last_seen.iso8601()
        except AttributeError:  # TODO: This logic belongs somewhere - anywhere - else.
            last_seen = str(node.last_seen)  # In case it's the constant NEVER_SEEN

        fleet_icon = node.fleet_state_nickname_metadata
        if fleet_icon is UNKNOWN_FLEET_STATE:
            fleet_icon = "?"  # TODO
        else:
            fleet_icon = fleet_icon[0][1]

        return {"icon_details": node.nickname_icon_details(),  # TODO: Mix this in better.
                "rest_url": node.rest_url(),
                "nickname": node.nickname,
                "checksum_address": node.worker_address,
                "staker_address": node.checksum_address,
                "timestamp": node.timestamp.iso8601(),
                "last_seen": last_seen,
                "fleet_state_icon": fleet_icon,
                }


class NodeSprout(PartiallyKwargifiedBytes):
    """
    An abridged node class designed for optimization of instantiation of > 100 nodes simultaneously.
    """
    verified_node = False

    def __init__(self, node_metadata):
        super().__init__(node_metadata)
        self.checksum_address = to_checksum_address(node_metadata['public_address'][0])
        self.nickname = nickname_from_seed(self.checksum_address)[0]
        self.timestamp = maya.MayaDT(int.from_bytes(node_metadata['timestamp'][0], byteorder="big"))
        self._hash = int.from_bytes(bytes(node_metadata['verifying_key'][0]), byteorder="big")

    def __hash__(self):
        return self._hash

    def __repr__(self):
        r = f"({self.__class__.__name__})⇀{self.nickname}↽ ({self.checksum_address})"
        return r

    def __bytes__(self):
        b = super().__bytes__()

        # We assume that the TEACHER_VERSION of this codebase is the version for this NodeSprout.
        # This is probably true, right?  Might need to be re-examined someday if we have
        # different node types of different versions.
        version = Teacher.TEACHER_VERSION.to_bytes(2, "big")
        return version + b

    @property
    def stamp(self) -> bytes:
        return self.processed_objects['verifying_key'][0]

    def mature(self):
        mature_node = self.finish()

        # As long as we're doing egregious workarounds, here's another one.  # TODO: 1481
        filepath = mature_node._cert_store_function(certificate=mature_node.certificate)
        mature_node.certificate_filepath = filepath

        self.__class__ = mature_node.__class__
        self.__dict__ = mature_node.__dict__


class Learner:
    """
    Any participant in the "learning loop" - a class inheriting from
    this one has the ability, synchronously or asynchronously,
    to learn about nodes in the network, verify some essential
    details about them, and store information about them for later use.
    """

    _SHORT_LEARNING_DELAY = 5
    _LONG_LEARNING_DELAY = 90
    LEARNING_TIMEOUT = 10
    _ROUNDS_WITHOUT_NODES_AFTER_WHICH_TO_SLOW_DOWN = 10

    # For Keeps
    __DEFAULT_NODE_STORAGE = ForgetfulNodeStorage
    __DEFAULT_MIDDLEWARE_CLASS = RestMiddleware

    LEARNER_VERSION = LEARNING_LOOP_VERSION
    node_splitter = BytestringSplitter(VariableLengthBytestring)
    version_splitter = BytestringSplitter((int, 2, {"byteorder": "big"}))
    tracker_class = FleetStateTracker

    invalid_metadata_message = "{} has invalid metadata.  The node's stake may have ended, or it is transitioning to a new interface. Ignoring."
    unknown_version_message = "{} purported to be of version {}, but we're only version {}.  Is there a new version of NuCypher?"
    really_unknown_version_message = "Unable to glean address from node that perhaps purported to be version {}.  We're only version {}."
    fleet_state_icon = ""

    class NotEnoughNodes(RuntimeError):
        pass

    class NotEnoughTeachers(NotEnoughNodes):
        pass

    class UnresponsiveTeacher(ConnectionError):
        pass

    class NotATeacher(ValueError):
        """
        Raised when a character cannot be properly utilized because
        it does not have the proper attributes for learning or verification.
        """

    class InvalidSignature(Exception):
        pass

    def __init__(self,
                 domains: set,
                 node_class: object = None,
                 network_middleware: RestMiddleware = __DEFAULT_MIDDLEWARE_CLASS(),
                 start_learning_now: bool = False,
                 learn_on_same_thread: bool = False,
                 known_nodes: tuple = None,
                 seed_nodes: Tuple[tuple] = None,
                 node_storage=None,
                 save_metadata: bool = False,
                 abort_on_learning_error: bool = False,
                 lonely: bool = False
                 ) -> None:

        self.log = Logger("learning-loop")  # type: Logger

        self.learning_domains = domains
        self.network_middleware = network_middleware
        self.save_metadata = save_metadata
        self.start_learning_now = start_learning_now
        self.learn_on_same_thread = learn_on_same_thread

        self._abort_on_learning_error = abort_on_learning_error
        self._learning_listeners = defaultdict(list)
        self._node_ids_to_learn_about_immediately = set()

        self.__known_nodes = self.tracker_class()

        self.lonely = lonely
        self.done_seeding = False

        if not node_storage:
            # Fallback storage backend
            node_storage = self.__DEFAULT_NODE_STORAGE(federated_only=self.federated_only)
        self.node_storage = node_storage
        if save_metadata and node_storage is NO_STORAGE_AVAILIBLE:
            raise ValueError("Cannot save nodes without a configured node storage")

        self.node_class = node_class or Teacher
        self.node_class.set_cert_storage_function(node_storage.store_node_certificate)  #  TODO: Fix this temporary workaround for on-disk cert storage.

        known_nodes = known_nodes or tuple()
        self.unresponsive_startup_nodes = list()  # TODO: Buckets - Attempt to use these again later
        for node in known_nodes:
            try:
                self.remember_node(node, eager=True)
            except self.UnresponsiveTeacher:
                self.unresponsive_startup_nodes.append(node)

        self.teacher_nodes = deque()
        self._current_teacher_node = None  # type: Teacher
        self._learning_task = task.LoopingCall(self.keep_learning_about_nodes)
        self._learning_round = 0  # type: int
        self._rounds_without_new_nodes = 0  # type: int
        self._seed_nodes = seed_nodes or []
        self.unresponsive_seed_nodes = set()

        if self.start_learning_now:
            self.start_learning_loop(now=self.learn_on_same_thread)

    @property
    def known_nodes(self):
        return self.__known_nodes

    def load_seednodes(self, read_storage: bool = True, retry_attempts: int = 3):
        """
        Engage known nodes from storages and pre-fetch hardcoded seednode certificates for node learning.
        """
        if self.done_seeding:
            self.log.debug("Already done seeding; won't try again.")
            return

        from nucypher.characters.lawful import Ursula
        for seednode_metadata in self._seed_nodes:

            self.log.debug(
                "Seeding from: {}|{}:{}".format(seednode_metadata.checksum_address,
                                                seednode_metadata.rest_host,
                                                seednode_metadata.rest_port))

            seed_node = Ursula.from_seednode_metadata(seednode_metadata=seednode_metadata,
                                                      network_middleware=self.network_middleware,
                                                      federated_only=self.federated_only)  # TODO: 466
            if seed_node is False:
                self.unresponsive_seed_nodes.add(seednode_metadata)
            else:
                self.unresponsive_seed_nodes.discard(seednode_metadata)
                self.remember_node(seed_node)

        if not self.unresponsive_seed_nodes:
            self.log.info("Finished learning about all seednodes.")

        self.done_seeding = True

        if read_storage is True:
            self.read_nodes_from_storage()

        if not self.known_nodes:
            self.log.warn("No seednodes were available after {} attempts".format(retry_attempts))
            # TODO: Need some actual logic here for situation with no seed nodes (ie, maybe try again much later)

    def read_nodes_from_storage(self) -> None:
        stored_nodes = self.node_storage.all(federated_only=self.federated_only)  # TODO: #466
        for node in stored_nodes:
            self.remember_node(node)

    def remember_node(self,
                      node,
                      force_verification_recheck=False,
                      record_fleet_state=True,
                      eager: bool = False,
                      grow_node_sprout_into_node=False):

        # UNPARSED
        # PARSED
        # METADATA_CHECKED
        # VERIFIED_CERT
        # VERIFIED_STAKE

        if node == self:  # No need to remember self.
            return False

        # First, determine if this is an outdated representation of an already known node.
        # TODO: #1032
        with suppress(KeyError):
            already_known_node = self.known_nodes[node.checksum_address]
            if not node.timestamp > already_known_node.timestamp:
                self.log.debug("Skipping already known node {}".format(already_known_node))
                # This node is already known.  We can safely return.
                return False

        self.known_nodes[node.checksum_address] = node

        if self.save_metadata:
            self.node_storage.store_node_metadata(node=node)

        try:
            stranger_certificate = node.certificate
        except AttributeError:
            # Probably a sprout.
            try:
                if grow_node_sprout_into_node:
                    node.mature()
                    stranger_certificate = node.certificate
                else:
                    # TODO: Well, why?  What about eagerness, popping listeners, etc?  We not doing that stuff?
                    return node
            except Exception as e:
                # Whoops, we got an Alice, Bob, or something totally wrong...
                raise self.NotATeacher(f"{node.__class__.__name__} does not have a certificate and cannot be remembered.")

        # Store node's certificate - It has been seen.
        certificate_filepath = self.node_storage.store_node_certificate(certificate=stranger_certificate)

        # In some cases (seed nodes or other temp stored certs),
        # this will update the filepath from the temp location to this one.
        node.certificate_filepath = certificate_filepath

        self.log.info(f"Saved TLS certificate for {node.nickname}: {certificate_filepath}")
        if eager:
            try:
                node.verify_node(force=force_verification_recheck,
                                 network_middleware_client=self.network_middleware.client,
                                 registry=self.registry)  # composed on character subclass, determines operating mode
            except SSLError:
                # TODO: Bucket this node as having bad TLS info - maybe it's an update that hasn't fully propagated?
                return False

            except NodeSeemsToBeDown:
                self.log.info("No Response while trying to verify node {}|{}".format(node.rest_interface, node))
                # TODO: Bucket this node as "ghost" or something: somebody else knows about it, but we can't get to it.
                return False

            except node.NotStaking:
                # TODO: Bucket this node as inactive, and potentially safe to forget.
                self.log.info(f'Staker:Worker {node.checksum_address}:{node.worker_address} is not actively staking, skipping.')
                return False

        listeners = self._learning_listeners.pop(node.checksum_address, tuple())

        self.log.info(
            "Remembering {} ({}), popping {} listeners.".format(node.nickname, node.checksum_address, len(listeners)))
        for listener in listeners:
            listener.add(node.checksum_address)
        self._node_ids_to_learn_about_immediately.discard(node.checksum_address)

        if record_fleet_state:
            self.known_nodes.record_fleet_state()

        return node

    def start_learning_loop(self, now=False):
        if self._learning_task.running:
            return False
        elif now:
            self.log.info("Starting Learning Loop NOW.")

            if self.lonely:
                self.done_seeding = True
                self.read_nodes_from_storage()

            else:
                self.load_seednodes()

            self.learn_from_teacher_node()
            self.learning_deferred = self._learning_task.start(interval=self._SHORT_LEARNING_DELAY)
            self.learning_deferred.addErrback(self.handle_learning_errors)
            return self.learning_deferred
        else:
            self.log.info("Starting Learning Loop.")

            learning_deferreds = list()
            if not self.lonely:
                seeder_deferred = deferToThread(self.load_seednodes)
                seeder_deferred.addErrback(self.handle_learning_errors)
                learning_deferreds.append(seeder_deferred)

            learner_deferred = self._learning_task.start(interval=self._SHORT_LEARNING_DELAY, now=now)
            learner_deferred.addErrback(self.handle_learning_errors)
            learning_deferreds.append(learner_deferred)

            self.learning_deferred = defer.DeferredList(learning_deferreds)
            return self.learning_deferred

    def stop_learning_loop(self, reason=None):
        """
        Only for tests at this point.  Maybe some day for graceful shutdowns.
        """
        self._learning_task.stop()

    def handle_learning_errors(self, *args, **kwargs):
        failure = args[0]
        if self._abort_on_learning_error:
            self.log.critical("Unhandled error during node learning.  Attempting graceful crash.")
            reactor.callFromThread(self._crash_gracefully, failure=failure)
        else:
            cleaned_traceback = failure.getTraceback().replace('{', '').replace('}', '')  # FIXME: Amazing.
            self.log.warn("Unhandled error during node learning: {}".format(cleaned_traceback))
            if not self._learning_task.running:
                self.start_learning_loop()  # TODO: Consider a single entry point for this with more elegant pause and unpause.

    def _crash_gracefully(self, failure=None):
        """
        A facility for crashing more gracefully in the event that an exception
        is unhandled in a different thread, especially inside a loop like the learning loop.
        """
        self._crashed = failure
        failure.raiseException()
        # TODO: We don't actually have checksum_address at this level - maybe only Characters can crash gracefully :-)
        self.log.critical("{} crashed with {}".format(self.checksum_address, failure))

    def select_teacher_nodes(self):
        nodes_we_know_about = self.known_nodes.shuffled()

        if not nodes_we_know_about:
            raise self.NotEnoughTeachers("Need some nodes to start learning from.")

        self.teacher_nodes.extend(nodes_we_know_about)

    def cycle_teacher_node(self):
        # To ensure that all the best teachers are available, first let's make sure
        # that we have connected to all the seed nodes.
        if self.unresponsive_seed_nodes and not self.lonely:
            self.log.info("Still have unresponsive seed nodes; trying again to connect.")
            self.load_seednodes()  # Ideally, this is async and singular.

        if not self.teacher_nodes:
            self.select_teacher_nodes()
        try:
            self._current_teacher_node = self.teacher_nodes.pop()
        except IndexError:
            error = "Not enough nodes to select a good teacher, Check your network connection then node configuration"
            raise self.NotEnoughTeachers(error)
        self.log.info("Cycled teachers; New teacher is {}".format(self._current_teacher_node))

    def current_teacher_node(self, cycle=False):
        if cycle:
            self.cycle_teacher_node()

        if not self._current_teacher_node:
            self.cycle_teacher_node()

        teacher = self._current_teacher_node

        return teacher

    def learn_about_nodes_now(self, force=False):
        if self._learning_task.running:
            self._learning_task.reset()
            self._learning_task()
        elif not force:
            self.log.warn(
                "Learning loop isn't started; can't learn about nodes now.  You can override this with force=True.")
        elif force:
            self.log.info("Learning loop wasn't started; forcing start now.")
            self._learning_task.start(self._SHORT_LEARNING_DELAY, now=True)

    def keep_learning_about_nodes(self):
        """
        Continually learn about new nodes.
        """
        # TODO: Allow the user to set eagerness?
        self.learn_from_teacher_node(eager=False)

    def learn_about_specific_nodes(self, addresses: Set):
        self._node_ids_to_learn_about_immediately.update(addresses)  # hmmmm
        self.learn_about_nodes_now()

    # TODO: Dehydrate these next two methods.

    def block_until_number_of_known_nodes_is(self,
                                             number_of_nodes_to_know: int,
                                             timeout: int = 10,
                                             learn_on_this_thread: bool = False,
                                             eager: bool = False):
        start = maya.now()
        starting_round = self._learning_round

        while True:
            rounds_undertaken = self._learning_round - starting_round
            if len(self.known_nodes) >= number_of_nodes_to_know:
                if rounds_undertaken:
                    self.log.info("Learned about enough nodes after {} rounds.".format(rounds_undertaken))
                return True

            if not self._learning_task.running:
                self.log.warn("Blocking to learn about nodes, but learning loop isn't running.")
            if learn_on_this_thread:
                try:
                    self.learn_from_teacher_node(eager=eager)
                except (requests.exceptions.ReadTimeout, requests.exceptions.ConnectTimeout):
                    # TODO: Even this "same thread" logic can be done off the main thread.
                    self.log.warn("Teacher was unreachable.  No good way to handle this on the main thread.")

            # The rest of the fucking owl
            round_finish = maya.now()
            if (round_finish - start).seconds > timeout:
                if not self._learning_task.running:
                    raise RuntimeError("Learning loop is not running.  Start it with start_learning().")
                else:
                    raise self.NotEnoughNodes("After {} seconds and {} rounds, didn't find {} nodes".format(
                        timeout, rounds_undertaken, number_of_nodes_to_know))
            else:
                time.sleep(.1)

    def block_until_specific_nodes_are_known(self,
                                             addresses: Set,
                                             timeout=LEARNING_TIMEOUT,
                                             allow_missing=0,
                                             learn_on_this_thread=False):
        start = maya.now()
        starting_round = self._learning_round

        while True:
            if self._crashed:
                return self._crashed
            rounds_undertaken = self._learning_round - starting_round
            if addresses.issubset(self.known_nodes.addresses()):
                if rounds_undertaken:
                    self.log.info("Learned about all nodes after {} rounds.".format(rounds_undertaken))
                return True

            if not self._learning_task.running:
                self.log.warn("Blocking to learn about nodes, but learning loop isn't running.")
            if learn_on_this_thread:
                self.learn_from_teacher_node(eager=True)

            if (maya.now() - start).seconds > timeout:

                still_unknown = addresses.difference(self.known_nodes.addresses())

                if len(still_unknown) <= allow_missing:
                    return False
                elif not self._learning_task.running:
                    raise self.NotEnoughTeachers("The learning loop is not running.  Start it with start_learning().")
                else:
                    raise self.NotEnoughTeachers(
                        "After {} seconds and {} rounds, didn't find these {} nodes: {}".format(
                            timeout, rounds_undertaken, len(still_unknown), still_unknown))
            else:
                time.sleep(.1)

    def _adjust_learning(self, node_list):
        """
        Takes a list of new nodes, adjusts learning accordingly.

        Currently, simply slows down learning loop when no new nodes have been discovered in a while.
        TODO: Do other important things - scrub, bucket, etc.
        """
        if node_list:
            self._rounds_without_new_nodes = 0
            self._learning_task.interval = self._SHORT_LEARNING_DELAY
        else:
            self._rounds_without_new_nodes += 1
            if self._rounds_without_new_nodes > self._ROUNDS_WITHOUT_NODES_AFTER_WHICH_TO_SLOW_DOWN:
                self.log.info("After {} rounds with no new nodes, it's time to slow down to {} seconds.".format(
                    self._ROUNDS_WITHOUT_NODES_AFTER_WHICH_TO_SLOW_DOWN,
                    self._LONG_LEARNING_DELAY))
                self._learning_task.interval = self._LONG_LEARNING_DELAY

    def _push_certain_newly_discovered_nodes_here(self, queue_to_push, node_addresses):
        """
        If any node_addresses are discovered, push them to queue_to_push.
        """
        for node_address in node_addresses:
            self.log.info("Adding listener for {}".format(node_address))
            self._learning_listeners[node_address].append(queue_to_push)

    def network_bootstrap(self, node_list: list) -> None:
        for node_addr, port in node_list:
            new_nodes = self.learn_about_nodes_now(node_addr, port)
            self.__known_nodes.update(new_nodes)

    def get_nodes_by_ids(self, node_ids):
        for node_id in node_ids:
            try:
                # Scenario 1: We already know about this node.
                return self.__known_nodes[node_id]
            except KeyError:
                raise NotImplementedError
        # Scenario 2: We don't know about this node, but a nearby node does.
        # TODO: Build a concurrent pool of lookups here.

        # Scenario 3: We don't know about this node, and neither does our friend.

    def write_node_metadata(self, node, serializer=bytes) -> str:
        return self.node_storage.store_node_metadata(node=node)

    def verify_from(self,
                    stranger: 'Teacher',
                    message_kit: Union[UmbralMessageKit, bytes],
                    signature: Signature):
        #
        # Optional Sanity Check
        #

        # In the spirit of duck-typing, we want to accept a message kit object, or bytes
        # If the higher-order object MessageKit is passed, we can perform an additional
        # eager sanity check before performing decryption.

        with contextlib.suppress(AttributeError):
            sender_verifying_key = stranger.stamp.as_umbral_pubkey()
            if message_kit.sender_verifying_key:
                if not message_kit.sender_verifying_key == sender_verifying_key:
                    raise ValueError("This MessageKit doesn't appear to have come from {}".format(stranger))
        message = bytes(message_kit)

        #
        # Verify Signature
        #

        if signature:
            is_valid = signature.verify(message, sender_verifying_key)
            if not is_valid:
                raise self.InvalidSignature("Signature for message isn't valid: {}".format(signature))
        else:
            raise self.InvalidSignature("No signature provided -- signature presumed invalid.")

    def learn_from_teacher_node(self, eager=False):
        """
        Sends a request to node_url to find out about known nodes.
        """
        self._learning_round += 1

        try:
            current_teacher = self.current_teacher_node()
        except self.NotEnoughTeachers as e:
            self.log.warn("Can't learn right now: {}".format(e.args[0]))
            return

        if Teacher in self.__class__.__bases__:
            announce_nodes = [self]
        else:
            announce_nodes = None

        unresponsive_nodes = set()

        #
        # Request
        #

        try:
            response = self.network_middleware.get_nodes_via_rest(node=current_teacher,
                                                                  nodes_i_need=self._node_ids_to_learn_about_immediately,
                                                                  announce_nodes=announce_nodes,
                                                                  fleet_checksum=self.known_nodes.checksum)
        except NodeSeemsToBeDown as e:
            unresponsive_nodes.add(current_teacher)
            self.log.info("Bad Response from teacher: {}:{}.".format(current_teacher, e))
            return

        finally:
            # Is cycling happening in the right order?
            self.cycle_teacher_node()

        # Before we parse the response, let's handle some edge cases.
        if response.status_code == 204:
            # In this case, this node knows about no other nodes.  Hopefully we've taught it something.
            if response.content == b"":
                return NO_KNOWN_NODES
            # In the other case - where the status code is 204 but the repsonse isn't blank - we'll keep parsing.
            # It's possible that our fleet states match, and we'll check for that later.

        elif response.status_code != 200:
            self.log.info("Bad response from teacher {}: {} - {}".format(current_teacher, response, response.content))
            return


        if not set(self.learning_domains).intersection(set(current_teacher.serving_domains)):
            _domains = ",".join(current_teacher.serving_domains)
            self.log.debug(
                f"{current_teacher} is serving {_domains}, which we aren't learning.")
            return  # This node is not serving any of our domains.


        #
        # Deserialize
        #
        try:
            signature, node_payload = signature_splitter(response.content, return_remainder=True)
        except BytestringSplittingError as e:
            self.log.warn("No signature prepended to Teacher {} payload: {}".format(current_teacher, response.content))
            return

        try:
            self.verify_from(current_teacher, node_payload, signature=signature)
        except current_teacher.InvalidSignature:
            # TODO: What to do if the teacher improperly signed the node payload?
            raise

        # End edge case handling.
        fleet_state_checksum_bytes, fleet_state_updated_bytes, node_payload = FleetStateTracker.snapshot_splitter(
            node_payload,
            return_remainder=True)

        current_teacher.last_seen = maya.now()
        # TODO: This is weird - let's get a stranger FleetState going.
        checksum = fleet_state_checksum_bytes.hex()

        # TODO: This doesn't make sense - a decentralized node can still learn about a federated-only node.
        if constant_or_bytes(node_payload) is FLEET_STATES_MATCH:
            current_teacher.update_snapshot(checksum=checksum,
                                            updated=maya.MayaDT(
                                                int.from_bytes(fleet_state_updated_bytes, byteorder="big")),
                                            number_of_known_nodes=len(self.known_nodes))
            return FLEET_STATES_MATCH

        # Note: There was previously a version check here, but that required iterating through node bytestrings twice,
        # so it has been removed.  When we create a new Ursula bytestring version, let's put the check
        # somewhere more performant, like mature() or verify_node().

        sprouts = self.node_class.batch_from_bytes(node_payload)
        remembered = []
        for sprout in sprouts:
            fail_fast = True  # TODO
            try:
                node_or_false = self.remember_node(sprout,
                                                   record_fleet_state=False,
                                                   # Do we want both of these to be decided by `eager`?
                                                   eager=eager,
                                                   grow_node_sprout_into_node=eager)
                if node_or_false is not False:
                    remembered.append(node_or_false)

                #
                # Report Failure
                #

            except NodeSeemsToBeDown:
                self.log.info(f"Verification Failed - "
                              f"Cannot establish connection to {node}.")

            except sprout.StampNotSigned:
                self.log.warn(f'Verification Failed - '
                              f'{sprout} stamp is unsigned.')

            except sprout.NotStaking:
                self.log.warn(f'Verification Failed - '
                              f'{sprout} has no active stakes in the current period '
                              f'({self.staking_agent.get_current_period()}')

            except sprout.InvalidWorkerSignature:
                self.log.warn(f'Verification Failed - '
                              f'{sprout} has an invalid wallet signature for {sprout.decentralized_identity_evidence}')

            except sprout.DetachedWorker:
                self.log.warn(f'Verification Failed - '
                              f'{sprout} is not bonded to a Staker.')

            except sprout.Invalidsprout:
                self.log.warn(sprout.invalid_metadata_message.format(sprout))

            except sprout.SuspiciousActivity:
                message = f"Suspicious Activity: Discovered sprout with bad signature: {sprout}." \
                          f"Propagated by: {current_teacher}"
                self.log.warn(message)


        # Is cycling happening in the right order?
        current_teacher.update_snapshot(checksum=checksum,
                                        updated=maya.MayaDT(int.from_bytes(fleet_state_updated_bytes, byteorder="big")),
                                        number_of_known_nodes=len(sprouts))

        ###################


        learning_round_log_message = "Learning round {}.  Teacher: {} knew about {} nodes, {} were new."
        self.log.info(learning_round_log_message.format(self._learning_round,
                                                        current_teacher,
                                                        len(sprouts),
                                                        len(remembered)))
        if remembered:
            self.known_nodes.record_fleet_state()
        return sprouts


class Teacher:
    TEACHER_VERSION = LEARNING_LOOP_VERSION
    _interface_info_splitter = (int, 4, {'byteorder': 'big'})
    log = Logger("teacher")
    __DEFAULT_MIN_SEED_STAKE = 0

    def __init__(self,
                 domains: Set,
                 certificate: Certificate,
                 certificate_filepath: str,
                 interface_signature=NOT_SIGNED.bool_value(False),
                 timestamp=NOT_SIGNED,
                 decentralized_identity_evidence=NOT_SIGNED,
                 ) -> None:

        #
        # Fleet
        #

        self.serving_domains = domains
        self.fleet_state_checksum = None
        self.fleet_state_updated = None
        self.last_seen = NEVER_SEEN("No Connection to Node")

        self.fleet_state_icon = UNKNOWN_FLEET_STATE
        self.fleet_state_nickname = UNKNOWN_FLEET_STATE
        self.fleet_state_nickname_metadata = UNKNOWN_FLEET_STATE

        #
        # Identity
        #

        self._timestamp = timestamp
        self.certificate = certificate
        self.certificate_filepath = certificate_filepath
        self.__interface_signature = interface_signature
        self.__decentralized_identity_evidence = constant_or_bytes(decentralized_identity_evidence)

        # Assume unverified
        self.verified_stamp = False
        self.verified_worker = False
        self.verified_interface = False
        self.verified_node = False
        self.__worker_address = None

    class InvalidNode(SuspiciousActivity):
        """Raised when a node has an invalid characteristic - stamp, interface, or address."""

    class InvalidStamp(InvalidNode):
        """Base exception class for invalid character stamps"""

    class StampNotSigned(InvalidStamp):
        """Raised when a node does not have a stamp signature when one is required for verification"""

    class InvalidWorkerSignature(InvalidStamp):
        """Raised when a stamp fails signature verification or recovers an unexpected worker address"""

    class NotStaking(InvalidStamp):
        """Raised when a node fails verification because it is not currently staking"""

    class DetachedWorker(InvalidNode):
        """Raised when a node fails verification because it is not bonded to a Staker"""

    class WrongMode(TypeError):
        """Raised when a Character tries to use another Character as decentralized when the latter is federated_only."""

    class IsFromTheFuture(TypeError):
        """Raised when deserializing a Character from a future version."""

    @classmethod
    def set_cert_storage_function(cls, node_storage_function):
        cls._cert_store_function = node_storage_function

    def mature(self, *args, **kwargs):
        """
        This is the most mature form, so we do nothing.
        """

    def save_cert_for_this_stranger_node(stranger, certificate):
        return stranger._cert_store_function(certificate)

    @classmethod
    def set_federated_mode(cls, federated_only: bool):
        cls._federated_only_instances = federated_only

    @classmethod
    def from_tls_hosting_power(cls, tls_hosting_power: TLSHostingPower, *args, **kwargs) -> 'Teacher':
        certificate_filepath = tls_hosting_power.keypair.certificate_filepath
        certificate = tls_hosting_power.keypair.certificate
        return cls(certificate=certificate, certificate_filepath=certificate_filepath, *args, **kwargs)

    #
    # Known Nodes
    #

    def seed_node_metadata(self, as_teacher_uri=False):
        if as_teacher_uri:
            teacher_uri = f'{self.checksum_address}@{self.rest_server.rest_interface.host}:{self.rest_server.rest_interface.port}'
            return teacher_uri
        return SeednodeMetadata(self.checksum_address,  # type: str
                                self.rest_server.rest_interface.host,  # type: str
                                self.rest_server.rest_interface.port)  # type: int

    def sorted_nodes(self):
        nodes_to_consider = list(self.known_nodes.values()) + [self]
        return sorted(nodes_to_consider, key=lambda n: n.checksum_address)

    def bytestring_of_known_nodes(self):
        payload = self.known_nodes.snapshot()
        ursulas_as_vbytes = (VariableLengthBytestring(n) for n in self.known_nodes)
        ursulas_as_bytes = bytes().join(bytes(u) for u in ursulas_as_vbytes)
        ursulas_as_bytes += VariableLengthBytestring(bytes(self))

        payload += ursulas_as_bytes
        return payload

    def update_snapshot(self, checksum, updated, number_of_known_nodes):
        """
        TODO: We update the simple snapshot here, but of course if we're dealing
              with an instance that is also a Learner, it has
              its own notion of its FleetState, so we probably
              need a reckoning of sorts here to manage that.  In time.

        :param checksum:
        :param updated:
        :param number_of_known_nodes:
        :return:
        """
        self.fleet_state_nickname, self.fleet_state_nickname_metadata = nickname_from_seed(checksum, number_of_pairs=1)
        self.fleet_state_checksum = checksum
        self.fleet_state_updated = updated
        self.fleet_state_icon = icon_from_checksum(self.fleet_state_checksum,
                                                   nickname_metadata=self.fleet_state_nickname_metadata,
                                                   number_of_nodes=number_of_known_nodes)

    #
    # Stamp
    #

    def _stamp_has_valid_signature_by_worker(self) -> bool:
        """
        Off-chain Signature Verification of stamp signature by Worker's ETH account.
        Note that this only "certifies" the stamp with the worker's account,
        so it can be seen like a self certification. For complete assurance,
        it's necessary to validate on-chain the Staker-Worker relation.
        """
        if self.__decentralized_identity_evidence is NOT_SIGNED:
            return False
        signature_is_valid = verify_eip_191(message=bytes(self.stamp),
                                            signature=self.__decentralized_identity_evidence,
                                            address=self.worker_address)
        return signature_is_valid

    def _worker_is_bonded_to_staker(self, registry: BaseContractRegistry) -> bool:
        """
        This method assumes the stamp's signature is valid and accurate.
        As a follow-up, this checks that the worker is linked to a staker, but it may be
        the case that the "staker" isn't "staking" (e.g., all her tokens have been slashed).
        """
        # Lazy agent get or create
        staking_agent = ContractAgency.get_agent(StakingEscrowAgent, registry=registry)

        staker_address = staking_agent.get_staker_from_worker(worker_address=self.worker_address)
        if staker_address == BlockchainInterface.NULL_ADDRESS:
            raise self.DetachedWorker(f"Worker {self.worker_address} is detached")
        return staker_address == self.checksum_address

    def _staker_is_really_staking(self, registry: BaseContractRegistry) -> bool:
        """
        This method assumes the stamp's signature is valid and accurate.
        As a follow-up, this checks that the staker is, indeed, staking.
        """
        # Lazy agent get or create
        staking_agent = ContractAgency.get_agent(StakingEscrowAgent, registry=registry)  # type: StakingEscrowAgent

        try:
            economics = TokenEconomicsFactory.get_economics(registry=registry)
        except Exception:
            raise  # TODO: Get StandardEconomics

        min_stake = economics.minimum_allowed_locked

        stake_current_period = staking_agent.get_locked_tokens(staker_address=self.checksum_address, periods=0)
        stake_next_period = staking_agent.get_locked_tokens(staker_address=self.checksum_address, periods=1)
        is_staking = max(stake_current_period, stake_next_period) >= min_stake
        return is_staking

    def validate_worker(self, registry: BaseContractRegistry = None) -> None:

        # Federated
        if self.federated_only:
            message = "This node cannot be verified in this manner, " \
                      "but is OK to use in federated mode if you " \
                      "have reason to believe it is trustworthy."
            raise self.WrongMode(message)

        # Decentralized
        else:
            if self.__decentralized_identity_evidence is NOT_SIGNED:
                raise self.StampNotSigned

            # Off-chain signature verification
            if not self._stamp_has_valid_signature_by_worker():
                message = f"Invalid signature {self.__decentralized_identity_evidence.hex()} " \
                          f"from worker {self.worker_address} for stamp {bytes(self.stamp).hex()} "
                raise self.InvalidWorkerSignature(message)

            # On-chain staking check, if registry is present
            if registry:
                if not self._worker_is_bonded_to_staker(registry=registry):  # <-- Blockchain CALL
                    message = f"Worker {self.worker_address} is not bonded to staker {self.checksum_address}"
                    raise self.DetachedWorker(message)

                if self._staker_is_really_staking(registry=registry):  # <-- Blockchain CALL
                    self.verified_worker = True
                else:
                    raise self.NotStaking(f"Staker {self.checksum_address} is not staking")

            self.verified_stamp = True

    def validate_metadata(self, registry: BaseContractRegistry = None):

        # Verify the interface signature
        if not self.verified_interface:
            self.validate_interface()

        # Verify the identity evidence
        if self.verified_stamp:
            return

        # Offline check of valid stamp signature by worker
        try:
            self.validate_worker(registry=registry)
        except self.WrongMode:
            if bool(registry):
                raise

    def verify_node(self,
                    network_middleware_client,
                    registry: BaseContractRegistry = None,
                    certificate_filepath: str = None,
                    force: bool = False
                    ) -> bool:
        """
        Three things happening here:

        * Verify that the stamp matches the address (raises InvalidNode is it's not valid,
          or WrongMode if it's a federated mode and being verified as a decentralized node)

        * Verify the interface signature (raises InvalidNode if not valid)

        * Connect to the node, make sure that it's up, and that the signature and address we
          checked are the same ones this node is using now. (raises InvalidNode if not valid;
          also emits a specific warning depending on which check failed).

        """

        if force:
            self.verified_interface = False
            self.verified_node = False
            self.verified_stamp = False
            self.verified_worker = False

        if self.verified_node:
            return True

        if not registry and not self.federated_only:  # TODO: # 466
            self.log.debug("No registry provided for decentralized stranger node verification - "
                           "on-chain Staking verification will not be performed.")

        # This is both the stamp's client signature and interface metadata check; May raise InvalidNode
        self.validate_metadata(registry=registry)

        # The node's metadata is valid; let's be sure the interface is in order.
        if not certificate_filepath:
            if self.certificate_filepath is CERTIFICATE_NOT_SAVED:
                raise TypeError("We haven't saved a certificate for this node yet.")
            else:
                certificate_filepath = self.certificate_filepath

        response_data = network_middleware_client.node_information(host=self.rest_interface.host,
                                                            port=self.rest_interface.port,
                                                            certificate_filepath=certificate_filepath)

        version, node_bytes = self.version_splitter(response_data, return_remainder=True)

        sprout = self.internal_splitter(node_bytes, partial=True)

        # TODO: #589 - check timestamp here.

        verifying_keys_match = sprout['verifying_key'] == self.public_keys(SigningPower)
        encrypting_keys_match = sprout['encrypting_key'] == self.public_keys(DecryptingPower)
        addresses_match = sprout['public_address'] == self.canonical_public_address
        evidence_matches = sprout['decentralized_identity_evidence'] == self.__decentralized_identity_evidence

        if not all((encrypting_keys_match, verifying_keys_match, addresses_match, evidence_matches)):
            # Failure
            if not addresses_match:
                message = "Wallet address swapped out.  It appears that someone is trying to defraud this node."
            if not verifying_keys_match:
                message = "Verifying key swapped out.  It appears that someone is impersonating this node."
            else:
                message = "Wrong cryptographic material for this node - something fishy going on."
            # TODO: #355 - Optional reporting.
            raise self.InvalidNode(message)
        else:
            # Success
            self.verified_node = True

    @property
    def decentralized_identity_evidence(self):
        return self.__decentralized_identity_evidence

    @property
    def worker_address(self):
        if not self.__worker_address and not self.federated_only:
            if self.decentralized_identity_evidence is NOT_SIGNED:
                raise self.StampNotSigned  # TODO: Find a better exception
            self.__worker_address = recover_address_eip_191(message=bytes(self.stamp),
                                                            signature=self.decentralized_identity_evidence)
        return self.__worker_address

    def substantiate_stamp(self):
        transacting_power = self._crypto_power.power_ups(TransactingPower)
        signature = transacting_power.sign_message(message=bytes(self.stamp))
        self.__decentralized_identity_evidence = signature
        self.__worker_address = transacting_power.account

    #
    # Interface
    #

    def validate_interface(self) -> bool:
        """
        Checks that the interface info is valid for this node's canonical address.
        """
        interface_info_message = self._signable_interface_info_message()  # Contains canonical address.
        message = self.timestamp_bytes() + interface_info_message
        interface_is_valid = self._interface_signature.verify(message, self.public_keys(SigningPower))
        self.verified_interface = interface_is_valid
        if interface_is_valid:
            return True
        else:
            raise self.InvalidNode("Interface is not valid")

    def _signable_interface_info_message(self):
        message = self.canonical_public_address + self.rest_interface
        return message

    def _sign_and_date_interface_info(self):
        message = self._signable_interface_info_message()
        self._timestamp = maya.now()
        self.__interface_signature = self.stamp(self.timestamp_bytes() + message)

    @property
    def _interface_signature(self):
        if not self.__interface_signature:
            try:
                self._sign_and_date_interface_info()
            except NoSigningPower:
                raise NoSigningPower("This Ursula is a stranger and cannot be used to verify.")
        return self.__interface_signature

    @property
    def timestamp(self):
        if not self._timestamp:
            try:
                self._sign_and_date_interface_info()
            except NoSigningPower:
                raise NoSigningPower("This Node is a Stranger; you didn't init with a timestamp, so you can't verify.")
        return self._timestamp

    def timestamp_bytes(self):
        return self.timestamp.epoch.to_bytes(4, 'big')

    #
    # Nicknames
    #

    @property
    def nickname_icon(self):
        return '{} {}'.format(self.nickname_metadata[0][1], self.nickname_metadata[1][1])

    def nickname_icon_html(self):
        icon_template = """
        <div class="nucypher-nickname-icon" style="border-top-color:{first_color}; border-left-color:{first_color}; border-bottom-color:{second_color}; border-right-color:{second_color};">
        <span class="small">{known_node_class} v{version}</span>
        <div class="symbols">
            <span class="single-symbol" style="color: {first_color}">{first_symbol}&#xFE0E;</span>
            <span class="single-symbol" style="color: {second_color}">{second_symbol}&#xFE0E;</span>
        </div>
        <br/>
        <span class="small-address">{address_first6}</span>
        </div>
        """.replace("  ", "").replace('\n', "")
        return icon_template.format(**self.nickname_icon_details)

    def nickname_icon_details(self):
        return dict(
            node_class=self.__class__.__name__,
            version=self.TEACHER_VERSION,
            first_color=self.nickname_metadata[0][0]['hex'],  # TODO: These index lookups are awful.
            first_symbol=self.nickname_metadata[0][1],
            second_color=self.nickname_metadata[1][0]['hex'],
            second_symbol=self.nickname_metadata[1][1],
            address_first6=self.checksum_address[2:8]
        )
