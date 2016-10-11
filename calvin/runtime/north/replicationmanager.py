# -*- coding: utf-8 -*-

# Copyright (c) 2015 Ericsson AB
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import copy

from calvin.requests import calvinresponse
from calvin.utilities.calvin_callback import CalvinCB
from calvin.utilities.calvinuuid import uuid
from calvin.utilities.calvinlogger import get_logger
from calvin.actor.actorstate import ActorState

_log = get_logger(__name__)

class ReplicationData(object):
    """An actors replication data"""
    def __init__(self, actor_id=None, master=None, requirements=None, initialize=True):
        super(ReplicationData, self).__init__()
        self.id = uuid("REPLICATION") if initialize else None
        self.master = master
        self.instances = [] if actor_id is None else [actor_id]
        # TODO requirements should be plugin operation, now just do target number
        self.requirements = requirements
        self.counter = 0

    def state(self, remap=None):
        state = {}
        if self.id is not None:
            # Replicas only need to keep track of id, master actor and their count number
            # Other data need to be synced from registry anyway when e.g. switching master
            state['id'] = self.id
            state['master'] = self.master
            state['counter'] = self.counter
            if remap is None:
                # For normal migration include these
                state['instances'] = self.instances
                state['requirements'] = self.requirements
        return state

    def set_state(self, state):
        self.id = state.get('id', None)
        self.master = state.get('master', None)
        self.instances = state.get('instances', [])
        self.requirements = state.get('requirements', {})
        self.counter = state.get('counter', 0)

    def add_replica(self, actor_id):
        if actor_id in self.instances:
            return
        self.instances.append(actor_id)
        self.counter += 1

    def get_replicas(self, when_master=None):
        if self.id and self.instances and (when_master is None or when_master == self.master):
            return [a for a in self.instances if a != self.master]
        else:
            return []

class ReplicationManager(object):
    def __init__(self, node):
        super(ReplicationManager, self).__init__()
        self.node = node

    def supervise_actor(self, actor_id, requirements):
        try:
            actor = self.node.am.actors[actor_id]
        except:
            return calvinresponse.CalvinResponse(calvinresponse.NOT_FOUND)

        if actor._replication_data.id is None:
            actor._replication_data = ReplicationData(
                actor_id=actor_id, master=actor_id, requirements=requirements)
        else:
            return calvinresponse.CalvinResponse(calvinresponse.BAD_REQUEST)

        # TODO add a callback to make sure storing worked
        self.node.storage.add_replication(actor._replication_data, cb=None)
        #TODO trigger replication loop
        return calvinresponse.CalvinResponse(True)

    def list_master_actors(self):
        return [a_id for a_id, a in self.node.am.actors.items() if a._replication_data.master == a_id]

    def replicate(self, actor_id, dst_node_id, callback):
        actor = self.node.am.actors[actor_id]
        if actor._replication_data.id is None or actor._replication_data.master != actor.id:
            # Only replicate master actor
            raise Exception("Only replicate master actor")
        _log.analyze(self.node.id, "+", actor._replication_data.state(None))
        # TODO make name a property that combine name and counter in actor
        new_id = uuid("ACTOR")
        actor._replication_data.add_replica(new_id)
        new_name = actor.name + "/{}".format(actor._replication_data.counter)
        actor_type = actor._type
        ports = actor.connections(self.node.id)
        ports['actor_name'] = new_name
        ports['actor_id'] = new_id
        remap_ports = {pid: uuid("PORT") for pid in ports['inports'].keys() + ports['outports'].keys()}
        ports['inports'] = {remap_ports[pid]: v for pid, v in ports['inports'].items()}
        ports['outports'] = {remap_ports[pid]: v for pid, v in ports['outports'].items()}
        _log.analyze(self.node.id, "+ GET STATE", remap_ports)
        state = actor.state(remap_ports)
        state['name'] = new_name
        state['id'] = new_id
        actor.will_replicate(ActorState(state, actor._replication_data))
        if dst_node_id == self.node.id:
            # Make copies to make sure no objects are shared between actors
            state = copy.deepcopy(state)
            ports = copy.deepcopy(ports)
            self.node.am.new_from_migration(
                actor_type, state=state, prev_connections=ports, callback=CalvinCB(
                    self._replicated,
                    replication_id=actor._replication_data.id,
                    actor_id=new_id, callback=callback))
        else:
            self.node.proto.actor_new(
                dst_node_id, CalvinCB(self._replicated, replication_id=actor._replication_data.id,
                                         actor_id=new_id, callback=callback), actor_type, state, ports)

    def _replicated(self, status, replication_id=None, actor_id=None, callback=None):
        _log.analyze(self.node.id, "+", {'status': status, 'replication_id': replication_id, 'actor_id': actor_id})
        if status:
            # TODO add callback for storing
            self.node.storage.add_replica(replication_id, actor_id)
        if callback:
            status.data = {'actor_id': actor_id}
            callback(status)