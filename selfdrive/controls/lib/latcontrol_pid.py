import math
from selfdrive.config import Conversions as CV
from selfdrive.controls.lib.pid import PIController
from selfdrive.controls.lib.drive_helpers import get_steer_max
from cereal import log


class LatControlPID():
  def __init__(self, CP):
    self.pid = PIController((CP.lateralTuning.pid.kpBP, CP.lateralTuning.pid.kpV),
                            (CP.lateralTuning.pid.kiBP, CP.lateralTuning.pid.kiV),
                            k_f=CP.lateralTuning.pid.kf, pos_limit=1.0, neg_limit=-1.0,
                            sat_limit=CP.steerLimitTimer)

  def reset(self):
    self.pid.reset()

  def update(self, active, CS, CP, VM, params, desired_curvature, desired_curvature_rate):
    pid_log = log.ControlsState.LateralPIDState.new_message()
    pid_log.steeringAngleDeg = float(CS.steeringAngleDeg)
    pid_log.steeringRateDeg = float(CS.steeringRateDeg)

    angle_steers_des_no_offset_radians = VM.get_steer_from_curvature(-desired_curvature, CS.vEgo)
    angle_steers_des_no_offset = math.degrees(angle_steers_des_no_offset_radians)
    angle_steers_des = angle_steers_des_no_offset + params.angleOffsetDeg

    pid_log.angleError = angle_steers_des - CS.steeringAngleDeg 
    if CS.vEgo < 0.3 or not active:
      output_steer = 0.0
      pid_log.active = False
      self.pid.reset()
    else:
      steers_max = get_steer_max(CP, CS.vEgo)
      self.pid.pos_limit = steers_max
      self.pid.neg_limit = -steers_max

      # offset does not contribute to resistive torque
      # !!! VOLT ONLY SIGMOID !!! Solve your car's f(speed, angle) -> command.
      x = 2.73528719e-02 * angle_steers_des_no_offset
      sigmoid = x / (1 + math.fabs(x))
      steer_feedforward = 1.27699321e-03 * sigmoid * ((CS.vEgo + 2.29236858e+01) ** 2)

      deadzone = 0.0

      check_saturation = (CS.vEgo > 8) and not CS.steeringRateLimited and not CS.steeringPressed
      output_steer = self.pid.update(angle_steers_des, CS.steeringAngleDeg, check_saturation=check_saturation, override=CS.steeringPressed,
                                     feedforward=steer_feedforward, speed=CS.vEgo, deadzone=deadzone)
      pid_log.active = True
      pid_log.p = self.pid.p
      pid_log.i = self.pid.i
      pid_log.f = self.pid.f
      pid_log.output = output_steer
      pid_log.saturated = bool(self.pid.saturated)

    return output_steer, angle_steers_des, pid_log
