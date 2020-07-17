import argparse
import time
import msgpack
from enum import Enum, auto

import numpy as np

from planning_utils import a_star, heuristic, create_grid, prune_path
from udacidrone import Drone
from udacidrone.connection import MavlinkConnection
from udacidrone.messaging import MsgID
from udacidrone.frame_utils import global_to_local, local_to_global

import csv
import random
import matplotlib.pyplot as plt


class States(Enum):
    MANUAL = auto()
    ARMING = auto()
    TAKEOFF = auto()
    WAYPOINT = auto()
    LANDING = auto()
    DISARMING = auto()
    PLANNING = auto()


class MotionPlanning(Drone):

    def __init__(self, connection, global_goal=None):
        super().__init__(connection)

        self.target_position = np.array([0.0, 0.0, 0.0])
        self.waypoints = []
        self.in_mission = True
        self.check_state = {}

        self.global_goal = global_goal

        # initial state
        self.flight_state = States.MANUAL

        # register all your callbacks here
        self.register_callback(MsgID.LOCAL_POSITION, self.local_position_callback)
        self.register_callback(MsgID.LOCAL_VELOCITY, self.velocity_callback)
        self.register_callback(MsgID.STATE, self.state_callback)

    def local_position_callback(self):
        if self.flight_state == States.TAKEOFF:
            if -1.0 * self.local_position[2] > 0.95 * self.target_position[2]:
                self.waypoint_transition()
        elif self.flight_state == States.WAYPOINT:
            if np.linalg.norm(self.target_position[0:2] - self.local_position[0:2]) < 1.0:
                if len(self.waypoints) > 0:
                    self.waypoint_transition()
                else:
                    if np.linalg.norm(self.local_velocity[0:2]) < 1.0:
                        self.landing_transition()

    def velocity_callback(self):
        if self.flight_state == States.LANDING:
            if self.global_position[2] - self.global_home[2] < 0.1:
                if abs(self.local_position[2]) < 0.01:
                    self.disarming_transition()

    def state_callback(self):
        if self.in_mission:
            if self.flight_state == States.MANUAL:
                self.arming_transition()
            elif self.flight_state == States.ARMING:
                if self.armed:
                    self.plan_path()
            elif self.flight_state == States.PLANNING:
                self.takeoff_transition()
            elif self.flight_state == States.DISARMING:
                if ~self.armed & ~self.guided:
                    self.manual_transition()

    def arming_transition(self):
        self.flight_state = States.ARMING
        print("arming transition")
        self.arm()
        self.take_control()

    def takeoff_transition(self):
        self.flight_state = States.TAKEOFF
        print("takeoff transition")
        self.takeoff(self.target_position[2])

    def waypoint_transition(self):
        self.flight_state = States.WAYPOINT
        print("waypoint transition")
        self.target_position = self.waypoints.pop(0)
        print('target position', self.target_position)
        self.cmd_position(self.target_position[0], self.target_position[1], self.target_position[2], self.target_position[3])

    def landing_transition(self):
        self.flight_state = States.LANDING
        print("landing transition")
        self.land()

    def disarming_transition(self):
        self.flight_state = States.DISARMING
        print("disarm transition")
        self.disarm()
        self.release_control()

    def manual_transition(self):
        self.flight_state = States.MANUAL
        print("manual transition")
        self.stop()
        self.in_mission = False

    def send_waypoints(self):
        print("Sending waypoints to simulator ...")
        data = msgpack.dumps(self.waypoints)
        self.connection._master.write(data)

    def plan_path(self):
        self.flight_state = States.PLANNING
        print("Searching for a path ...")
        TARGET_ALTITUDE = 5
        SAFETY_DISTANCE = 5

        self.target_position[2] = TARGET_ALTITUDE

        # read lat0, lon0 from colliders into floating point values
        csv_init_param = []
        with open('colliders.csv') as csvFile:
            reader = csv.reader(csvFile)
            lat_lon_str = next(reader)
            csv_init_param = dict(pair.split() for pair in lat_lon_str)
            print("lat lon init params: ", csv_init_param)

        lat0 = float(csv_init_param["lat0"])
        lon0 = float(csv_init_param["lon0"])

        # set home position to (lon0, lat0, 0)
        self.set_home_position(lon0, lat0, 0)
        # retrieve current global position        
        # convert to current local position using global_to_local()
        local_north, local_east, local_down = global_to_local(self.global_position, self.global_home)
        print('global home {0}, position {1}, local position {2}'.format(self.global_home, self.global_position,
                                                                         self.local_position))
        # Read in obstacle map
        data = np.loadtxt('colliders.csv', delimiter=',', dtype='Float64', skiprows=2)
        
        # Define a grid for a particular altitude and safety margin around obstacles
        grid, north_offset, east_offset = create_grid(data, TARGET_ALTITUDE, SAFETY_DISTANCE)
        print("North offset = {0}, east offset = {1}".format(north_offset, east_offset))

        # Define starting point on the grid (this is just grid center)
        grid_start = (int(np.ceil(local_north-north_offset)), int(np.ceil(local_east-east_offset)))

        # convert start position to current position rather than map center        

        # adapt to set goal as latitude / longitude position and convert
        grid_goal = None
        if self.global_goal is not None:
            print("global goal:", global_goal)
            local_goal_north, local_goal_east, local_goal_down = global_to_local(global_goal, self.global_home)
            grid_goal = (int(np.ceil(local_goal_north-north_offset)) , int(np.ceil(local_goal_east-east_offset)))
            
            if (grid[grid_goal[0],grid_goal[1]]==1):
                print("goal :", goal," is inside an obstruction. ")

        # Set goal as some arbitrary position on the grid if goal is not set
        if grid_goal is None:
            #random goal which is unobstructed.
            grid_goal = (int(random.uniform(0,grid.shape[0])), int(random.uniform(0, grid.shape[1])))
            while grid[grid_goal[0],grid_goal[1]]==1:
                grid_goal = (int(random.uniform(0,grid.shape[0])), int(random.uniform(0, grid.shape[1])))

        # Run A* to find a path from start to goal
        # add diagonal motions with a cost of sqrt(2) to your A* implementation
        # or move to a different search space such as a graph (not done here)
        print('Local Start and Goal: ', grid_start, grid_goal)
        path, _ = a_star(grid, heuristic, grid_start, grid_goal)
        #  prune path to minimize number of waypoints
        # TODO : (if you're feeling ambitious): Try a different approach altogether!
        new_path = prune_path(grid, path)

        # Convert path to waypoints
        #waypoints = [[p[0] + north_offset, p[1] + east_offset, TARGET_ALTITUDE, 0] for p in new_path]

        #adding heading command
        waypoints = []
        for p in new_path:
            waypoint_north = p[0] + north_offset
            waypoint_east  = p[1] + east_offset
            if len(waypoints)>0:
                last_waypoint = waypoints[-1]
                heading = np.arctan2((waypoint_east-last_waypoint[1]), (waypoint_north-last_waypoint[0]))
            else:
                heading=0
            waypoints.append([waypoint_north, waypoint_east, TARGET_ALTITUDE, heading])

        print("waypoints:", waypoints)

        #display the path on map
        plt.imshow(grid, origin='lower') 
        plt.plot(grid_start[1], grid_start[0], 'x')
        plt.plot(grid_goal[1], grid_goal[0], 'x')

        if path is not None:
            pp = np.array(path)
            plt.plot(pp[:, 1], pp[:, 0], 'g')
        plt.xlabel('EAST')
        plt.ylabel('NORTH')
        plt.draw()
        plt.pause(0.001)

        # Set self.waypoints
        self.waypoints = waypoints
        # send waypoints to sim (this is just for visualization of waypoints)
        self.send_waypoints()

    def start(self):
        self.start_log("Logs", "NavLog.txt")

        print("starting connection")
        self.connection.start()

        # Only required if they do threaded
        # while self.in_mission:
        #    pass

        self.stop_log()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=5760, help='Port number')
    parser.add_argument('--host', type=str, default='127.0.0.1', help="host address, i.e. '127.0.0.1'")
    parser.add_argument('--goal', type=str, default='', help="goal(lat, lon, alt), i.e. '-122.40195876, 37.79673913, -0.147'")
    args = parser.parse_args()

    global_goal= None
    if args.goal != '':
        print("--- goal: ", args.goal,"---")
        try:
            goal = [float(value) for value in args.goal.split(',')]
            if len(goal)==3:
                print(goal)
                global_goal = np.array(goal)
                print(global_goal)
        except Exception as err:
            print("************")
            print("Error occurred while setting global_goal. Global goal not set. ")
            print(err)
            print("************")
            global_goal =None


    conn = MavlinkConnection('tcp:{0}:{1}'.format(args.host, args.port), timeout=60*3)
    drone = MotionPlanning(conn, global_goal)
    time.sleep(1)

    drone.start()
