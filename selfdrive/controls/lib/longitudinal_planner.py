#!/usr/bin/env python3
import math
import numpy as np
from common.numpy_fast import interp

from common.params import Params
import cereal.messaging as messaging
from cereal import log
from common.realtime import DT_MDL
from common.realtime import sec_since_boot
from selfdrive.modeld.constants import T_IDXS
from selfdrive.config import Conversions as CV
from selfdrive.controls.lib.fcw import FCWChecker
from selfdrive.controls.lib.longcontrol import LongCtrlState
from selfdrive.controls.lib.lead_mpc import LeadMpc
from selfdrive.controls.lib.long_mpc import LongitudinalMpc
from selfdrive.controls.lib.limits_long_mpc import LimitsLongitudinalMpc
from selfdrive.controls.lib.drive_helpers import V_CRUISE_MAX, CONTROL_N
from selfdrive.controls.lib.vision_turn_controller import VisionTurnController
from selfdrive.controls.lib.speed_limit_controller import SpeedLimitController, SpeedLimitResolver
from selfdrive.controls.lib.turn_speed_controller import TurnSpeedController
from selfdrive.controls.lib.events import Events
from selfdrive.swaglog import cloudlog

LON_MPC_STEP = 0.2  # first step is 0.2s
AWARENESS_DECEL = -0.2     # car smoothly decel at .2m/s^2 when user is distracted

# lookup tables VS speed to determine min and max accels in cruise
# make sure these accelerations are smaller than mpc limits
_A_CRUISE_MIN_V_SPORT = [-3.0, -2.6, -2.3, -2.0, -1.0]
_A_CRUISE_MIN_V_FOLLOWING = [-3.0, -2.5, -2.0, -1.5, -1.0]
_A_CRUISE_MIN_V = [-2.0, -1.5, -1.0, -0.7, -0.5]
_A_CRUISE_MIN_BP = [0., 5., 10., 20., 55.]

# need fast accel at very low speed for stop and go
# make sure these accelerations are smaller than mpc limits
_A_CRUISE_MAX_V = [1.6, 1.8, 1.6, .6, .4]
_A_CRUISE_MAX_V_SPORT = [3.0, 3.2, 3.0, 2.0, 2.0]
_A_CRUISE_MAX_V_FOLLOWING = [2.0, 1.8, 2.0, .6, .4]
_A_CRUISE_MAX_BP = [0., 5., 10., 20., 55.]

_A_CRUISE_MIN_V_MODE_LIST = [_A_CRUISE_MIN_V, _A_CRUISE_MIN_V_SPORT]
_A_CRUISE_MAX_V_MODE_LIST = [_A_CRUISE_MAX_V, _A_CRUISE_MAX_V_SPORT]

# Lookup table for turns - fast accel
_A_TOTAL_MAX_V = [3.5, 4.0, 5.0]
_A_TOTAL_MAX_BP = [0., 25., 55.]

# Lookup table for turns original - used when slowOnCurves = 1 from kegman.json
_A_TOTAL_MAX_V_SOC = [1.7, 3.2]
_A_TOTAL_MAX_BP_SOC = [20., 40.]

def calc_cruise_accel_limits(v_ego, following, accelMode):
  if following:
    a_cruise_min = interp(v_ego, _A_CRUISE_MIN_BP, _A_CRUISE_MIN_V_FOLLOWING)
    a_cruise_max = interp(v_ego, _A_CRUISE_MAX_BP, _A_CRUISE_MAX_V_FOLLOWING)
  else:
    a_cruise_min = interp(v_ego, _A_CRUISE_MIN_BP, _A_CRUISE_MIN_V_MODE_LIST[accelMode])
    a_cruise_max = interp(v_ego, _A_CRUISE_MAX_BP, _A_CRUISE_MAX_V_MODE_LIST[accelMode])
  return [a_cruise_min, a_cruise_max]



def limit_accel_in_turns(v_ego, angle_steers, a_target, CP):
  """
  This function returns a limited long acceleration allowed, depending on the existing lateral acceleration
  this should avoid accelerating when losing the target in turns
  """

  a_total_max = interp(v_ego, _A_TOTAL_MAX_BP, _A_TOTAL_MAX_V)
  a_y = v_ego**2 * angle_steers * CV.DEG_TO_RAD / (CP.steerRatio * CP.wheelbase)
  a_x_allowed = math.sqrt(max(a_total_max**2 - a_y**2, 0.))

  return [a_target[0], min(a_target[1], a_x_allowed)]


class Planner():
  def __init__(self, CP):
    self.CP = CP
    self.mpcs = {}
    self.mpcs['lead0'] = LeadMpc(0)
    self.mpcs['lead1'] = LeadMpc(self.mpcs['lead0'].lead_id + 3)
    self.mpcs['cruise'] = LongitudinalMpc()
    self.mpcs['custom'] = LimitsLongitudinalMpc()

    self.fcw = False
    self.fcw_checker = FCWChecker()

    self.v_desired = 0.0
    self.a_desired = 0.0
    self.longitudinalPlanSource = 'cruise'
    self.alpha = np.exp(-DT_MDL/2.0)
    self.lead_0 = log.ModelDataV2.LeadDataV3.new_message()
    self.lead_1 = log.ModelDataV2.LeadDataV3.new_message()

    self.v_desired_trajectory = np.zeros(CONTROL_N)
    self.a_desired_trajectory = np.zeros(CONTROL_N)

    self.vision_turn_controller = VisionTurnController(CP)
    self.speed_limit_controller = SpeedLimitController()
    self.events = Events()
    self.turn_speed_controller = TurnSpeedController()
    
    self.accel_mode = int(Params().get_bool("SportAccel")) # 0 = normal, 1 = sport;

  def update(self, sm, CP):
    cur_time = sec_since_boot()
    v_ego = sm['carState'].vEgo
    a_ego = sm['carState'].aEgo

    v_cruise_kph = sm['controlsState'].vCruise
    v_cruise_kph = min(v_cruise_kph, V_CRUISE_MAX)
    v_cruise = v_cruise_kph * CV.KPH_TO_MS

    long_control_state = sm['controlsState'].longControlState
    force_slow_decel = sm['controlsState'].forceDecel

    self.lead_0 = sm['radarState'].leadOne
    self.lead_1 = sm['radarState'].leadTwo

    enabled = (long_control_state == LongCtrlState.pid) or (long_control_state == LongCtrlState.stopping)
    following = self.lead_1.status and self.lead_1.dRel < 45.0 and self.lead_1.vLeadK > v_ego and self.lead_1.aLeadK > 0.0
    if not enabled or sm['carState'].gasPressed:
      self.v_desired = v_ego
      self.a_desired = a_ego

    # Prevent divergence, smooth in current v_ego
    self.v_desired = self.alpha * self.v_desired + (1 - self.alpha) * v_ego
    self.v_desired = max(0.0, self.v_desired)

    # Get acceleration and active solutions for custom long mpc.
    a_mpc, active_mpc, c_source = self.mpc_solutions(enabled, self.v_desired, self.a_desired, v_cruise, sm)

    accel_limits = calc_cruise_accel_limits(v_ego, following, self.accel_mode)
    accel_limits_turns = limit_accel_in_turns(v_ego, sm['carState'].steeringAngleDeg, accel_limits, self.CP)
    if force_slow_decel:
      # if required so, force a smooth deceleration
      accel_limits_turns[1] = min(accel_limits_turns[1], AWARENESS_DECEL)
      accel_limits_turns[0] = min(accel_limits_turns[0], accel_limits_turns[1])

    # clip limits, cannot init MPC outside of bounds
    accel_limits_turns[0] = min(accel_limits_turns[0], self.a_desired)
    accel_limits_turns[1] = max(accel_limits_turns[1], self.a_desired)
    self.mpcs['cruise'].set_accel_limits(accel_limits_turns[0], accel_limits_turns[1])

    # ensure lower accel limit (for braking) is lower than target acc for custom controllers.
    accel_limits = [min(accel_limits_turns[0], a_mpc['custom']), accel_limits_turns[1]]
    self.mpcs['custom'].set_accel_limits(accel_limits[0], accel_limits[1])

    next_a = np.inf
    for key in self.mpcs:
      self.mpcs[key].set_cur_state(self.v_desired, self.a_desired)
      self.mpcs[key].update(sm['carState'], sm['radarState'], v_cruise, a_mpc[key], active_mpc[key])
      # picks slowest solution from accel in ~0.2 seconds
      if self.mpcs[key].status and active_mpc[key] and self.mpcs[key].a_solution[5] < next_a:
        self.longitudinalPlanSource = c_source if key == 'custom' else key
        self.v_desired_trajectory = self.mpcs[key].v_solution[:CONTROL_N]
        self.a_desired_trajectory = self.mpcs[key].a_solution[:CONTROL_N]
        self.j_desired_trajectory = self.mpcs[key].j_solution[:CONTROL_N]
        next_a = self.mpcs[key].a_solution[5]

    # determine fcw
    if self.mpcs['lead0'].new_lead:
      self.fcw_checker.reset_lead(cur_time)
    blinkers = sm['carState'].leftBlinker or sm['carState'].rightBlinker
    self.fcw = self.fcw_checker.update(self.mpcs['lead0'].mpc_solution, cur_time,
                                       sm['controlsState'].active,
                                       v_ego, sm['carState'].aEgo,
                                       self.lead_1.dRel, self.lead_1.vLead, self.lead_1.aLeadK,
                                       self.lead_1.yRel, self.lead_1.vLat,
                                       self.lead_1.fcw, blinkers) and not sm['carState'].brakePressed
    if self.fcw:
      cloudlog.info("FCW triggered %s", self.fcw_checker.counters)

    # Interpolate 0.05 seconds and save as starting point for next iteration
    a_prev = self.a_desired
    self.a_desired = float(interp(DT_MDL, T_IDXS[:CONTROL_N], self.a_desired_trajectory))
    self.v_desired = self.v_desired + DT_MDL * (self.a_desired + a_prev)/2.0

  def publish(self, sm, pm):
    plan_send = messaging.new_message('longitudinalPlan')

    plan_send.valid = sm.all_alive_and_valid(service_list=['carState', 'controlsState'])

    longitudinalPlan = plan_send.longitudinalPlan
    longitudinalPlan.modelMonoTime = sm.logMonoTime['modelV2']
    longitudinalPlan.processingDelay = (plan_send.logMonoTime / 1e9) - sm.logMonoTime['modelV2']

    longitudinalPlan.speeds = [float(x) for x in self.v_desired_trajectory]
    longitudinalPlan.accels = [float(x) for x in self.a_desired_trajectory]
    longitudinalPlan.jerks = [float(x) for x in self.j_desired_trajectory]

    longitudinalPlan.hasLead = self.mpcs['lead0'].status
    longitudinalPlan.longitudinalPlanSource = self.longitudinalPlanSource
    longitudinalPlan.fcw = self.fcw

    longitudinalPlan.visionTurnControllerState = self.vision_turn_controller.state
    longitudinalPlan.visionTurnSpeed = float(self.vision_turn_controller.v_turn)

    longitudinalPlan.speedLimitControlState = self.speed_limit_controller.state
    longitudinalPlan.speedLimit = float(self.speed_limit_controller.speed_limit)
    longitudinalPlan.speedLimitOffset = float(self.speed_limit_controller.speed_limit_offset)
    longitudinalPlan.distToSpeedLimit = float(self.speed_limit_controller.distance)
    longitudinalPlan.isMapSpeedLimit = bool(self.speed_limit_controller.source == SpeedLimitResolver.Source.map_data)
    longitudinalPlan.eventsDEPRECATED = self.events.to_msg()

    longitudinalPlan.turnSpeedControlState = self.turn_speed_controller.state
    longitudinalPlan.turnSpeed = float(self.turn_speed_controller.speed_limit)
    longitudinalPlan.distToTurn = float(self.turn_speed_controller.distance)
    longitudinalPlan.turnSign = int(self.turn_speed_controller.turn_sign)

    pm.send('longitudinalPlan', plan_send)

  def mpc_solutions(self, enabled, v_ego, a_ego, v_cruise, sm):
    # Update controllers
    self.vision_turn_controller.update(enabled, v_ego, a_ego, v_cruise, sm)
    self.events = Events()
    self.speed_limit_controller.update(enabled, v_ego, a_ego, sm, v_cruise, self.events)
    self.turn_speed_controller.update(enabled, v_ego, a_ego, sm)

    # Pick solution with lowest acceleration target.
    a_solutions = {None: float("inf")}

    if self.vision_turn_controller.is_active:
      a_solutions['turn'] = self.vision_turn_controller.a_target

    if self.speed_limit_controller.is_active:
      a_solutions['limit'] = self.speed_limit_controller.a_target

    if self.turn_speed_controller.is_active:
      a_solutions['turnlimit'] = self.turn_speed_controller.a_target

    source = min(a_solutions, key=a_solutions.get)

    a_sol = {
      'cruise': a_ego,  # Irrelevant
      'lead0': a_ego,   # Irrelevant
      'lead1': a_ego,   # Irrelevant
      'custom': 0. if source is None else a_solutions[source],
    }

    active_sol = {
      'cruise': True,  # Irrelevant
      'lead0': True,   # Irrelevant
      'lead1': True,   # Irrelevant
      'custom': source is not None,
    }

    return a_sol, active_sol, source
