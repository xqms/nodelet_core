#! /usr/bin/env python
# Copyright (c) 2009, Willow Garage, Inc.
# All rights reserved.
# 
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
# 
#     * Redistributions of source code must retain the above copyright
#       notice, this list of conditions and the following disclaimer.
#     * Redistributions in binary form must reproduce the above copyright
#       notice, this list of conditions and the following disclaimer in the
#       documentation and/or other materials provided with the distribution.
#     * Neither the name of the Willow Garage, Inc. nor the names of its
#       contributors may be used to endorse or promote products derived from
#       this software without specific prior written permission.
# 
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
# AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
# ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT OWNER OR CONTRIBUTORS BE
# LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR
# CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF
# SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS
# INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN
# CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
# ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
# POSSIBILITY OF SUCH DAMAGE.

# Author: Stuart Glaser
'''
Example:

roslib.load_manifest('move_base')
from move_base.msg import *
rospy.init_node('foo')


from move_base.msg import *
from geometry_msgs.msg import *
g1 = MoveBaseGoal(PoseStamped(Header(frame_id = 'base_link'),
                              Pose(Point(2, 0, 0),
                                   Quaternion(0, 0, 0, 1))))
g2 = MoveBaseGoal(PoseStamped(Header(frame_id = 'base_link'),
                              Pose(Point(5, 0, 0),
                                   Quaternion(0, 0, 0, 1))))

client = ActionClient('move_base', MoveBaseAction)

h1 = client.send_goal(g1)
h2 = client.send_goal(g2)
client.cancel_all_goals()
'''

import roslib; roslib.load_manifest('actionlib')
import threading
import weakref
import time
import rospy
from roslib.msg import Header
from actionlib_msgs.msg import *

g_goal_id = 1

class ActionException(Exception): pass

def get_name_of_constant(C, n):
    for k,v in C.__dict__.iteritems():
        if type(v) is int and v == n:
            return k
    return "NO_SUCH_STATE_%d" % n


class CommState(object):
    WAITING_FOR_GOAL_ACK = 0
    PENDING = 1
    ACTIVE = 2
    WAITING_FOR_RESULT = 3
    WAITING_FOR_CANCEL_ACK = 4
    RECALLING = 5
    PREEMPTING = 6
    DONE = 7

class TerminalState(object):
    RECALLED = GoalStatus.RECALLED
    REJECTED = GoalStatus.REJECTED
    PREEMPTED = GoalStatus.PREEMPTED
    ABORTED = GoalStatus.ABORTED
    SUCCEEDED = GoalStatus.SUCCEEDED
    LOST = GoalStatus.LOST


GoalStatus.to_string = classmethod(get_name_of_constant)
CommState.to_string = classmethod(get_name_of_constant)
TerminalState.to_string = classmethod(get_name_of_constant)

def _find_status_by_goal_id(status_array, id):
    for s in status_array.status_list:
        if s.goal_id.id == id:
            return s
    return None

## @brief Client side handle to monitor goal progress.
##
## A ClientGoalHandle is a reference counted object that is used to
## manipulate and monitor the progress of an already dispatched
## goal. Once all the goal handles go out of scope (or are reset), an
## ActionClient stops maintaining state for that goal.
class ClientGoalHandle:
    ## @brief Internal use only
    ##
    ## ClientGoalHandle objects should be created by the action
    ## client.  You should never need to construct one yourself.
    def __init__(self, comm_state_machine):
        self.comm_state_machine = comm_state_machine

        #print "GH created.  id = %.3f" % self.comm_state_machine.action_goal.goal_id.stamp.to_seconds()

    ## @brief True iff the two ClientGoalHandle's are tracking the same goal
    def __eq__(self, o):
        return self.comm_state_machine == o.comm_state_machine

    ## @brief Sends a cancel message for this specific goal to the ActionServer.
    ##
    ## Also transitions the client state to WAITING_FOR_CANCEL_ACK
    def cancel(self):
        self.comm_state_machine.mutex.acquire()
        try:
            cancel_msg = GoalID(stamp = rospy.Time(0),
                                id = self.comm_state_machine.action_goal.goal_id.id)
            self.comm_state_machine.send_cancel_fn(cancel_msg)
            self.comm_state_machine.transition_to(CommState.WAITING_FOR_CANCEL_ACK)
        finally:
            self.comm_state_machine.mutex.release()

    ## @brief Get the state of this goal's communication state machine from interaction with the server
    ## 
    ## Possible States are: WAITING_FOR_GOAL_ACK, PENDING, ACTIVE, WAITING_FOR_RESULT,
    ##                      WAITING_FOR_CANCEL_ACK, RECALLING, PREEMPTING, DONE
    ##
    ## @return The current goal's communication state with the server
    def get_comm_state(self):
        if not self.comm_state_machine:
            rospy.logerr("Trying to get_comm_state on an inactive ClientGoalHandle.")
            return CommState.LOST
        return self.comm_state_machine.state

    ## @brief Returns the current status of the goal.
    ##
    ## Possible states are listed in the enumeration in the
    ## actionlib_msgs/GoalStatus message.
    ##
    ## @return The current status of the goal.
    def get_goal_status(self):
        if not self.comm_state_machine:
            rospy.logerr("Trying to get_goal_status on an inactive ClientGoalHandle.")
            return GoalStatus.PENDING
        return self.comm_state_machine.latest_goal_status.status

    ## @brief Gets the result produced by the action server for this goal.
    ##
    ## @return None if no result was receieved.  Otherwise the goal's result as a *Result message.
    def get_result(self):
        if not self.comm_state_machine:
            rospy.logerr("Trying to get_result on an inactive ClientGoalHandle.")
            return None
        return self.comm_state_machine.latest_result.result

    ## @brief Gets the terminal state information for this goal.
    ##
    ## Possible States Are: RECALLED, REJECTED, PREEMPTED, ABORTED, SUCCEEDED, LOST
    ## This call only makes sense if CommState==DONE. This will send ROS_WARNs if we're not in DONE
    ##
    ## @return The terminal state as an integer from the GoalStatus message.
    def get_terminal_state(self):
        if not self.comm_state_machine:
            rospy.logerr("Trying to get_terminal_state on an inactive ClientGoalHandle.")
            return GoalStatus.LOST

        self.comm_state_machine.mutex.acquire()
        try:
            if self.comm_state_machine.state != CommState.DONE:
                rospy.logwarn("Asking for the terminal state when we're in [%s]",
                             ComState.to_string(self.comm_state_machine.state))

            goal_status = self.comm_state_machine.latest_goal_status.status
            if goal_status in [GoalStatus.PREEMPTED, GoalStatus.SUCCEEDED,
                               GoalStatus.ABORTED, GoalStatus.REJECTED,
                               GoalStatus.RECALLED, GoalStatus.LOST]:
                return goal_status

            rospy.logerr("Asking for a terminal state, but the goal status is %d", goal_status)
            return GoalStatus.LOST
        finally:
            self.comm_state_machine.mutex.release()


        
    
NO_TRANSITION = -1
INVALID_TRANSITION = -2
_transitions = { 
    CommState.WAITING_FOR_GOAL_ACK: {
        GoalStatus.PENDING:    CommState.PENDING,
        GoalStatus.ACTIVE:     CommState.ACTIVE,
        GoalStatus.REJECTED:   CommState.WAITING_FOR_RESULT,
        GoalStatus.RECALLING:  CommState.RECALLING,
        GoalStatus.RECALLED:   CommState.WAITING_FOR_RESULT,
        GoalStatus.PREEMPTED:  CommState.WAITING_FOR_RESULT,
        GoalStatus.SUCCEEDED:  CommState.WAITING_FOR_RESULT,
        GoalStatus.ABORTED:    CommState.WAITING_FOR_RESULT,
        GoalStatus.PREEMPTING: CommState.PREEMPTING },
    CommState.PENDING: {
        GoalStatus.PENDING:    NO_TRANSITION,
        GoalStatus.ACTIVE:     CommState.ACTIVE,
        GoalStatus.REJECTED:   CommState.WAITING_FOR_RESULT,
        GoalStatus.RECALLING:  CommState.RECALLING,
        GoalStatus.RECALLED:   CommState.WAITING_FOR_RESULT,
        GoalStatus.PREEMPTED:  CommState.WAITING_FOR_RESULT,
        GoalStatus.SUCCEEDED:  CommState.WAITING_FOR_RESULT,
        GoalStatus.ABORTED:    CommState.WAITING_FOR_RESULT,
        GoalStatus.PREEMPTING: CommState.PREEMPTING },
    CommState.ACTIVE: {
        GoalStatus.PENDING:    INVALID_TRANSITION,
        GoalStatus.ACTIVE:     NO_TRANSITION,
        GoalStatus.REJECTED:   INVALID_TRANSITION,
        GoalStatus.RECALLING:  INVALID_TRANSITION,
        GoalStatus.RECALLED:   INVALID_TRANSITION,
        GoalStatus.PREEMPTED:  CommState.WAITING_FOR_RESULT,
        GoalStatus.SUCCEEDED:  CommState.WAITING_FOR_RESULT,
        GoalStatus.ABORTED:    CommState.WAITING_FOR_RESULT,
        GoalStatus.PREEMPTING: CommState.PREEMPTING },
    CommState.WAITING_FOR_RESULT: {
        GoalStatus.PENDING:    INVALID_TRANSITION,
        GoalStatus.ACTIVE:     NO_TRANSITION,
        GoalStatus.REJECTED:   NO_TRANSITION,
        GoalStatus.RECALLING:  INVALID_TRANSITION,
        GoalStatus.RECALLED:   NO_TRANSITION,
        GoalStatus.PREEMPTED:  NO_TRANSITION,
        GoalStatus.SUCCEEDED:  NO_TRANSITION,
        GoalStatus.ABORTED:    NO_TRANSITION,
        GoalStatus.PREEMPTING: INVALID_TRANSITION },
    CommState.WAITING_FOR_CANCEL_ACK: {
        GoalStatus.PENDING:    NO_TRANSITION,
        GoalStatus.ACTIVE:     NO_TRANSITION,
        GoalStatus.REJECTED:   CommState.WAITING_FOR_RESULT,
        GoalStatus.RECALLING:  CommState.RECALLING,
        GoalStatus.RECALLED:   CommState.WAITING_FOR_RESULT,
        GoalStatus.PREEMPTED:  CommState.WAITING_FOR_RESULT,
        GoalStatus.SUCCEEDED:  CommState.WAITING_FOR_RESULT,
        GoalStatus.ABORTED:    CommState.WAITING_FOR_RESULT,
        GoalStatus.PREEMPTING: CommState.PREEMPTING },
    CommState.RECALLING: {
        GoalStatus.PENDING:    INVALID_TRANSITION,
        GoalStatus.ACTIVE:     INVALID_TRANSITION,
        GoalStatus.REJECTED:   CommState.WAITING_FOR_RESULT,
        GoalStatus.RECALLING:  NO_TRANSITION,
        GoalStatus.RECALLED:   CommState.WAITING_FOR_RESULT,
        GoalStatus.PREEMPTED:  CommState.WAITING_FOR_RESULT,
        GoalStatus.SUCCEEDED:  CommState.WAITING_FOR_RESULT,
        GoalStatus.ABORTED:    CommState.WAITING_FOR_RESULT,
        GoalStatus.PREEMPTING: CommState.PREEMPTING },
    CommState.PREEMPTING: {
        GoalStatus.PENDING:    INVALID_TRANSITION,
        GoalStatus.ACTIVE:     INVALID_TRANSITION,
        GoalStatus.REJECTED:   INVALID_TRANSITION,
        GoalStatus.RECALLING:  INVALID_TRANSITION,
        GoalStatus.RECALLED:   INVALID_TRANSITION,
        GoalStatus.PREEMPTED:  CommState.WAITING_FOR_RESULT,
        GoalStatus.SUCCEEDED:  CommState.WAITING_FOR_RESULT,
        GoalStatus.ABORTED:    CommState.WAITING_FOR_RESULT,
        GoalStatus.PREEMPTING: NO_TRANSITION },
    CommState.DONE: {
        GoalStatus.PENDING:    INVALID_TRANSITION,
        GoalStatus.ACTIVE:     INVALID_TRANSITION,
        GoalStatus.REJECTED:   NO_TRANSITION,
        GoalStatus.RECALLING:  INVALID_TRANSITION,
        GoalStatus.RECALLED:   NO_TRANSITION,
        GoalStatus.PREEMPTED:  NO_TRANSITION,
        GoalStatus.SUCCEEDED:  NO_TRANSITION,
        GoalStatus.ABORTED:    NO_TRANSITION,
        GoalStatus.PREEMPTING: INVALID_TRANSITION } }



class CommStateMachine:
    def __init__(self, action_goal, transition_cb, feedback_cb, send_goal_fn, send_cancel_fn):
        self.action_goal = action_goal
        self.transition_cb = transition_cb
        self.feedback_cb = feedback_cb
        self.send_goal_fn = send_goal_fn
        self.send_cancel_fn = send_cancel_fn
        
        self.state = CommState.WAITING_FOR_GOAL_ACK
        self.mutex = threading.RLock()
        self.latest_goal_status = GoalStatus(status = GoalStatus.PENDING)
        self.latest_result = None

    def set_state(self, state):
        rospy.logdebug("Transitioning CommState from %s to %s",
                       CommState.to_string(self.state), CommState.to_string(state))
        self.state = state

    ##
    ## @param gh ClientGoalHandle
    ## @param status_array actionlib_msgs/GoalStatusArray
    def update_status(self, status_array):
        self.mutex.acquire()
        try:
            if self.state == CommState.DONE:
                return

            status = _find_status_by_goal_id(status_array, self.action_goal.goal_id.id)

            # You mean you haven't heard of me?
            if not status:
                if self.state not in [CommState.WAITING_FOR_GOAL_ACK,
                                      CommState.WAITING_FOR_RESULT,
                                      CommState.DONE]:
                    self._mark_as_lost()
                return

            self.latest_goal_status = status

            # Determines the next state from the lookup table
            if self.state not in _transitions:
                rospy.logerr("CommStateMachine is in a funny state: %i" % self.state)
                return
            if status.status not in _transitions[self.state]:
                rospy.logerr("Got an unknown status from the ActionServer: %i" % status.status)
                return
            next_state = _transitions[self.state][status.status]

            # Knowing the next state, what should we do?
            if next_state == NO_TRANSITION:
                pass
            elif next_state == INVALID_TRANSITION:
                rospy.logerr("Invalid goal status transition from %s to %s" %
                             (CommState.to_string(self.state), GoalStatus.to_string(status.status)))
            else:
                self.transition_to(next_state)
                
        finally:
            self.mutex.release()

    def transition_to(self, state):
        rospy.logdebug("Transitioning to %s (from %s, goal: %s)",
                       CommState.to_string(state), CommState.to_string(self.state),
                       self.action_goal.goal_id.id)
        self.state = state
        if self.transition_cb:
            self.transition_cb(ClientGoalHandle(self))
            
    def _mark_as_lost(self):
        self.latest_goal_status.status = GoalStatus.LOST
        self.transition_to(CommState.DONE)

    def update_result(self, action_result):
        # Might not be for us
        if self.action_goal.goal_id.id != action_result.status.goal_id.id:
            return

        self.mutex.acquire()
        try:
            self.latest_goal_status = action_result.status
            self.latest_result = action_result

            if self.state in [CommState.WAITING_FOR_GOAL_ACK,
                              CommState.PENDING,
                              CommState.ACTIVE,
                              CommState.WAITING_FOR_RESULT,
                              CommState.RECALLING,
                              CommState.PREEMPTING]:
                self.transition_to(CommState.DONE)
            elif self.state == CommState.DONE:
                rospy.logerr("Got a result when we were already in the DONE state")
            else:
                rospy.logerr("In a funny state: %i" % self.state)
            
        finally:
            self.mutex.release()

    def update_feedback(self, action_feedback):
        # Might not be for us
        if self.action_goal.goal_id.id != action_feedback.status.goal_id.id:
            return

        if self.feedback_cb:
            self.feedback_cb(action_feedback.feedback)


class GoalManager:

    # statuses - a list of weak references to CommStateMachine objects

    def __init__(self, ActionSpec):
        self.list_mutex = threading.RLock()
        self.statuses = []
        self.send_goal_fn = None

        try:
            a = ActionSpec()
            
            self.ActionSpec = ActionSpec
            self.ActionGoal = type(a.action_goal)
            self.ActionResult = type(a.action_result)
            self.ActionFeedback = type(a.action_feedback)
        except AttributeError:
            raise ActionException("Type is not an action spec: %s" % str(ActionSpec))

    def _generate_id(self):
        global g_goal_id
        id, g_goal_id = g_goal_id, g_goal_id + 1
        now = rospy.Time.now()
        return GoalID(id = "%s-%i" % (rospy.get_caller_id(), id), stamp = now)

    def register_send_goal_fn(self, fn):
        self.send_goal_fn = fn
    def register_cancel_fn(self, fn):
        self.cancel_fn = fn

    ## Sends off a goal and starts tracking its status.
    ##
    ## @return ClientGoalHandle for the sent goal.
    def init_goal(self, goal, transition_cb = None, feedback_cb = None):
        action_goal = self.ActionGoal(header = Header(),
                                      goal_id = self._generate_id(),
                                      goal = goal)

        csm = CommStateMachine(action_goal, transition_cb, feedback_cb,
                               self.send_goal_fn, self.cancel_fn)

        self.list_mutex.acquire()
        try:
            self.statuses.append(weakref.ref(csm))
        finally:
            self.list_mutex.release()

        self.send_goal_fn(action_goal)

        return ClientGoalHandle(csm)


    # Pulls out the statuses that are still live (creating strong
    # references to them)
    def _get_live_statuses(self):
        self.list_mutex.acquire()
        try:
            live_statuses = [r() for r in self.statuses]
            live_statuses = filter(lambda x: x, live_statuses)
            return live_statuses
        finally:
            self.list_mutex.release()

        
    ## Updates the statuses of all goals from the information in status_array.
    ## 
    ## @param status_array (\c actionlib_msgs/GoalStatusArray)
    def update_statuses(self, status_array):
        live_statuses = []
        
        self.list_mutex.acquire()
        try:
            # Garbage collects dead status objects
            self.statuses = [r for r in self.statuses if r()]
        finally:
            self.list_mutex.release()

        for status in self._get_live_statuses():
            status.update_status(status_array)


    def update_results(self, action_result):
        for status in self._get_live_statuses():
            status.update_result(action_result)

    def update_feedbacks(self, action_feedback):
        for status in self._get_live_statuses():
            status.update_feedback(action_feedback)

class ActionClient:
    ## @brief Constructs an ActionClient and opens connections to an ActionServer.
    ##
    ## @param ns The namespace in which to access the action.  For
    ## example, the "goal" topic should occur under ns/goal
    ##
    ## @param ActionSpec The *Action message type.  The ActionClient
    ## will grab the other message types from this type.
    def __init__(self, ns, ActionSpec):
        self.ns = ns
        self.last_status_msg = None

        try:
            a = ActionSpec()
            
            self.ActionSpec = ActionSpec
            self.ActionGoal = type(a.action_goal)
            self.ActionResult = type(a.action_result)
            self.ActionFeedback = type(a.action_feedback)
        except AttributeError:
            raise ActionException("Type is not an action spec: %s" % str(ActionSpec))

        self.pub_goal = rospy.Publisher(ns + '/goal', self.ActionGoal)
        self.pub_cancel = rospy.Publisher(ns + '/cancel', GoalID)

        self.manager = GoalManager(ActionSpec)
        self.manager.register_send_goal_fn(self.pub_goal.publish)
        self.manager.register_cancel_fn(self.pub_cancel.publish)
       
        rospy.Subscriber(ns + '/status', GoalStatusArray, self._status_cb)
        rospy.Subscriber(ns + '/result', self.ActionResult, self._result_cb)
        rospy.Subscriber(ns + '/feedback', self.ActionFeedback, self._feedback_cb)

    ## @brief Sends a goal to the action server
    ##
    ## @param goal An instance of the *Goal message.
    ##
    ## @param transition_cb Callback that gets called on every client
    ## state transition for the sent goal.  It should take in a
    ## ClientGoalHandle as an argument.
    ## 
    ## @param feedback_cb Callback that gets called every time
    ## feedback is received for the sent goal.  It takes two
    ## parameters: a ClientGoalHandle and an instance of the *Feedback
    ## message.
    ## 
    ## @return ClientGoalHandle for the sent goal.
    def send_goal(self, goal, transition_cb = None, feedback_cb = None):
        return self.manager.init_goal(goal, transition_cb, feedback_cb)

    ## @brief Cancels all goals currently running on the action server.
    ##
    ## Preempts all goals running on the action server at the point
    ## that the cancel message is serviced by the action server.
    def cancel_all_goals(self):
        cancel_msg = GoalID(stamp = rospy.Time.from_seconds(0.0),
                            id = rospy.Time.from_seconds(0.0))
        self.pub_cancel.publish(cancel_msg)

    ## @brief Waits for the ActionServer to connect to this client
    ##
    ## Often, it can take a second for the action server & client to negotiate
    ## a connection, thus, risking the first few goals to be dropped. This call lets
    ## the user wait until the network connection to the server is negotiated
    def wait_for_action_server_to_start(self, timeout = ros.Duration(0.0)):
        started = False
        timeout_time = rospy.get_rostime() + timeout
        while rospy.get_rostime() < timeout_time and not rospy.is_shutdown():
            if self.last_status_msg:
                server_id = self.last_status_msg._connection_header['callerid']

                if self.pub_goal.impl.has_connection(server_id) and \
                        self.pub_cancel.impl.has_connection(server_id):
                    started = True
                    break

            time.sleep(0.01)
                
        return started

    def _status_cb(self, msg):
        self.last_status_msg = msg
        self.manager.update_statuses(msg)

    def _result_cb(self, msg):
        self.manager.update_results(msg)

    def _feedback_cb(self, msg):
        self.manager.update_feedbacks(msg)

