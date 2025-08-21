#!/usr/bin/env python3
"""
FAULT DETECTOR NODE - IMPROVED VERSION
"""

import rclpy
from rclpy.node import Node
from rclpy.qos import QoSProfile, ReliabilityPolicy, HistoryPolicy, DurabilityPolicy
from px4_msgs.msg import (
    EstimatorStatusFlags, SensorGps, VehicleStatus, VehicleCommand, 
    VehicleCommandAck, VehicleImuStatus, SensorMag, SensorBaro,
    VehicleAirData, RcChannels, VehicleGlobalPosition
)
from std_msgs.msg import String
import time
from threading import Lock
from collections import deque
import json

class FaultDetector(Node):
    def __init__(self):
        super().__init__('fault_detector')

        # Parameters for sensor health monitoring
        self.declare_parameters(
            namespace='',
            parameters=[
                # GPS parameters
                ('gps_timeout', 30.0),
                ('min_satellites', 8),
                ('min_fix_type', 3),
                ('gps_failure_count_threshold', 6),
                
                # IMU parameters
                ('imu_timeout', 5.0),
                ('max_imu_inconsistency', 0.5),  # rad/s or m/s^2
                
                # Magnetometer parameters
                ('mag_timeout', 10.0),
                ('max_mag_inconsistency', 0.3),  # gauss
                
                # Barometer parameters
                ('baro_timeout', 10.0),
                
                # RC parameters
                ('rc_timeout', 10.0),
                ('min_rc_quality', 50),  # percentage
                
                # Estimator parameters
                ('estimator_timeout', 30.0),
                
                # General parameters
                ('check_interval', 5.0),
                ('startup_grace_period', 45.0),
                ('mission_start_grace_period', 45.0),
                ('min_flight_time_before_emergency', 30.0),
                ('min_altitude_for_emergency', 30.0),
                ('command_cooldown', 15.0),
                ('sitl_mode', True),
                ('enable_sensor_checks', True),
            ]
        )

        # Get parameters
        self.gps_timeout = self.get_parameter('gps_timeout').value
        self.min_satellites = self.get_parameter('min_satellites').value
        self.min_fix_type = self.get_parameter('min_fix_type').value
        self.gps_failure_threshold = self.get_parameter('gps_failure_count_threshold').value
        self.imu_timeout = self.get_parameter('imu_timeout').value
        self.max_imu_inconsistency = self.get_parameter('max_imu_inconsistency').value
        self.mag_timeout = self.get_parameter('mag_timeout').value
        self.max_mag_inconsistency = self.get_parameter('max_mag_inconsistency').value
        self.baro_timeout = self.get_parameter('baro_timeout').value
        self.rc_timeout = self.get_parameter('rc_timeout').value
        self.min_rc_quality = self.get_parameter('min_rc_quality').value
        self.estimator_timeout = self.get_parameter('estimator_timeout').value
        self.check_interval = self.get_parameter('check_interval').value
        self.startup_grace = self.get_parameter('startup_grace_period').value
        self.mission_start_grace = self.get_parameter('mission_start_grace_period').value
        self.min_flight_time = self.get_parameter('min_flight_time_before_emergency').value
        self.min_altitude = self.get_parameter('min_altitude_for_emergency').value
        self.command_cooldown = self.get_parameter('command_cooldown').value
        self.sitl_mode = self.get_parameter('sitl_mode').value
        self.enable_sensor_checks = self.get_parameter('enable_sensor_checks').value

        # State variables
        self.sensor_data = {
            'gps': None,
            'imu': None,
            'mag': None,
            'baro': None,
            'rc': None,
            'estimator': None,
            'air_data': None
        }
        
        self.sensor_timestamps = {
            'gps': None,
            'imu': None,
            'mag': None,
            'baro': None,
            'rc': None,
            'estimator': None,
            'air_data': None
        }
        
        self.sensor_health = {
            'gps': True,
            'imu': True,
            'mag': True,
            'baro': True,
            'rc': True,
            'estimator': True,
            'air_data': True
        }
        
        self.vehicle_status = None
        self.global_position = None
        self.emergency_triggered = False
        self.emergency_trigger_source = None
        self.data_lock = Lock()
        self.node_start_time = time.time()
        self.armed_time = None
        self.mission_start_time = None
        self.mission_count = 0
        self.rtl_completed = False
        self.current_altitude = 0.0
        
        # Failure tracking
        self.consecutive_gps_failures = 0
        self.sensor_failure_counts = {sensor: 0 for sensor in self.sensor_health.keys()}
        self.last_health_log_time = 0
        
        # Command tracking
        self.last_emergency_command_time = 0
        self.pending_command_timestamp = None
        
        # GPS health history
        self.gps_health_history = deque(maxlen=10)
        self.last_good_gps_time = None

        # QoS profile
        px4_qos_profile = QoSProfile(
            reliability=ReliabilityPolicy.BEST_EFFORT,
            history=HistoryPolicy.KEEP_LAST,
            depth=1,
            durability=DurabilityPolicy.VOLATILE
        )

        # Publishers
        self.vehicle_command_pub = self.create_publisher(
            VehicleCommand,
            '/fmu/in/vehicle_command',
            px4_qos_profile
        )
        
        self.fault_status_pub = self.create_publisher(
            String,
            '/fault_detector/status',
            10
        )

        # Subscribers for various sensors
        self.create_subscription(
            SensorGps, '/fmu/out/vehicle_gps_position',
            self.gps_callback, qos_profile=px4_qos_profile
        )
        
        self.create_subscription(
            EstimatorStatusFlags, '/fmu/out/estimator_status_flags',
            self.estimator_callback, qos_profile=px4_qos_profile
        )
        
        self.create_subscription(
            VehicleStatus, '/fmu/out/vehicle_status',
            self.status_callback, qos_profile=px4_qos_profile
        )
        
        self.create_subscription(
            VehicleCommandAck, '/fmu/out/vehicle_command_ack',
            self.command_ack_callback, qos_profile=px4_qos_profile
        )
        
        self.create_subscription(
            VehicleGlobalPosition, '/fmu/out/vehicle_global_position',
            self.global_position_callback, qos_profile=px4_qos_profile
        )
        
        # Additional sensor subscriptions for real craft
        if self.enable_sensor_checks:
            self.create_subscription(
                VehicleImuStatus, '/fmu/out/vehicle_imu_status',
                self.imu_callback, qos_profile=px4_qos_profile
            )
            
            self.create_subscription(
                SensorMag, '/fmu/out/sensor_mag',
                self.mag_callback, qos_profile=px4_qos_profile
            )
            
            self.create_subscription(
                SensorBaro, '/fmu/out/sensor_baro',
                self.baro_callback, qos_profile=px4_qos_profile
            )
            
            self.create_subscription(
                VehicleAirData, '/fmu/out/vehicle_air_data',
                self.air_data_callback, qos_profile=px4_qos_profile
            )
            
            self.create_subscription(
                RcChannels, '/fmu/out/rc_channels',
                self.rc_callback, qos_profile=px4_qos_profile
            )

        # Timer for periodic checks
        self.create_timer(self.check_interval, self.check_system_health)
        
        # Status publisher timer (1Hz)
        self.create_timer(1.0, self.publish_status)

        self.print_startup_info()

    def print_startup_info(self):
        """Print startup configuration"""
        mode_str = "SITL MODE" if self.sitl_mode else "REAL CRAFT MODE"
        self.get_logger().info('='*60)
        self.get_logger().info(f'FAULT DETECTOR - SENSOR HEALTH MONITOR ({mode_str})')
        self.get_logger().info('='*60)
        self.get_logger().info(f'📡 Monitoring Sensors:')
        
        if self.sitl_mode:
            self.get_logger().info(f'  • GPS: 6+ sats, fix type ≥ 2 (relaxed for SITL)')
            self.get_logger().info(f'  • Failure threshold: {self.gps_failure_threshold*2} consecutive')
            self.get_logger().info(f'  • Sensor checks: Limited in SITL')
        else:
            self.get_logger().info(f'  • GPS: {self.min_satellites} sats, fix type ≥ {self.min_fix_type}')
            self.get_logger().info(f'  • Failure threshold: {self.gps_failure_threshold} consecutive')
            if self.enable_sensor_checks:
                self.get_logger().info(f'  • IMU: max inconsistency {self.max_imu_inconsistency}')
                self.get_logger().info(f'  • Magnetometer: max inconsistency {self.max_mag_inconsistency}')
                self.get_logger().info(f'  • Barometer: timeout {self.baro_timeout}s')
                self.get_logger().info(f'  • RC: quality ≥ {self.min_rc_quality}%')
        
        self.get_logger().info(f'  • Estimator: timeout {self.estimator_timeout}s')
        self.get_logger().info(f'🛡️ Safety Settings:')
        self.get_logger().info(f'  • Min altitude for RTL: {self.min_altitude}m')
        self.get_logger().info(f'  • Command cooldown: {self.command_cooldown}s')
        self.get_logger().info('='*60)

    def get_px4_mode_name(self, nav_state):
        """Get mode name from PX4 nav_state (same as power_monitor)"""
        mode_map = {
            0: "MANUAL",
            1: "ALTITUDE",
            2: "POSITION",
            3: "AUTO.MISSION",
            4: "AUTO.LOITER",
            5: "AUTO.RTL",
            6: "POSITION_SLOW",
            7: "FREE5",
            8: "FREE4",
            9: "FREE3",
            10: "ACRO",
            11: "FREE2",
            12: "DESCEND",
            13: "TERMINATION",
            14: "OFFBOARD",
            15: "STABILIZED",
            16: "FREE1",
            17: "AUTO.TAKEOFF",
            18: "AUTO.LAND",
            19: "AUTO.FOLLOW_TARGET",
            20: "AUTO.PRECLAND",
            21: "ORBIT",
            22: "AUTO.VTOL_TAKEOFF",
            23: "EXTERNAL1",
            24: "EXTERNAL2",
            25: "EXTERNAL3",
            26: "EXTERNAL4",
            27: "EXTERNAL5",
            28: "EXTERNAL6",
            29: "EXTERNAL7",
            30: "EXTERNAL8"
        }
        
        return mode_map.get(nav_state, f"UNKNOWN_{nav_state}")

    def get_current_mode(self):
        """Get current flight mode"""
        if not self.vehicle_status:
            return "UNKNOWN"
        return self.get_px4_mode_name(self.vehicle_status.nav_state)

    def gps_callback(self, msg):
        with self.data_lock:
            self.sensor_data['gps'] = msg
            
            # Check GPS health
            gps_healthy = (msg.fix_type >= self.min_fix_type and 
                          msg.satellites_used >= self.min_satellites)
            
            if gps_healthy:
                self.sensor_timestamps['gps'] = time.time()
                self.last_good_gps_time = time.time()
                self.consecutive_gps_failures = 0
                
            self.gps_health_history.append(gps_healthy)

    def estimator_callback(self, msg):
        with self.data_lock:
            self.sensor_data['estimator'] = msg
            self.sensor_timestamps['estimator'] = time.time()

    def imu_callback(self, msg):
        with self.data_lock:
            self.sensor_data['imu'] = msg
            self.sensor_timestamps['imu'] = time.time()

    def mag_callback(self, msg):
        with self.data_lock:
            self.sensor_data['mag'] = msg
            self.sensor_timestamps['mag'] = time.time()

    def baro_callback(self, msg):
        with self.data_lock:
            self.sensor_data['baro'] = msg
            self.sensor_timestamps['baro'] = time.time()

    def air_data_callback(self, msg):
        with self.data_lock:
            self.sensor_data['air_data'] = msg
            self.sensor_timestamps['air_data'] = time.time()

    def rc_callback(self, msg):
        with self.data_lock:
            self.sensor_data['rc'] = msg
            self.sensor_timestamps['rc'] = time.time()

    def global_position_callback(self, msg):
        with self.data_lock:
            self.global_position = msg
            if msg.alt:
                self.current_altitude = msg.alt

    def detect_mission_start(self, current_mode, previous_mode):
        """
        Unified mission start detection logic
        
        A NEW mission is detected when entering AUTO.MISSION or AUTO.TAKEOFF from:
        1. Manual control modes (MANUAL, POSITION, etc.)
        2. After landing (AUTO.LAND → AUTO.MISSION)
        3. After RTL completion (AUTO.RTL → AUTO.LOITER → AUTO.MISSION)
        4. Direct arming into mission mode
        
        NOT a new mission when:
        - Continuing from waypoint hold (LOITER without RTL)
        - Transitioning between AUTO.MISSION and AUTO.TAKEOFF
        """
        # Mission modes
        mission_modes = ['AUTO.MISSION', 'AUTO.TAKEOFF']
        
        # Non-auto modes that indicate pilot control
        manual_modes = ['MANUAL', 'ALTITUDE', 'POSITION', 'STABILIZED', 'ACRO', 'POSITION_SLOW']
        
        # Case 1: Fresh mission start from manual control
        if current_mode in mission_modes and previous_mode in manual_modes:
            return True, "Manual to Auto transition"
        
        # Case 2: Mission start after landing (but still armed)
        if (current_mode in mission_modes and 
            previous_mode in ['AUTO.LAND', 'AUTO.PRECLAND'] and
            self.armed_time):
            return True, "Mission restart after landing"
        
        # Case 3: Mission resume after RTL completion (loitering at home)
        if (current_mode in mission_modes and 
            previous_mode == 'AUTO.LOITER' and
            self.rtl_completed):
            self.rtl_completed = False  # Reset for next RTL
            return True, "Mission resume after RTL"
        
        # Case 4: First mission after arming directly into AUTO mode
        if (current_mode in mission_modes and 
            previous_mode is None and  # First mode after node start
            self.armed_time and
            time.time() - self.armed_time < 10):  # Recently armed
            return True, "Direct arm to mission"
        
        # Case 5: Mission continues (not a new mission)
        if current_mode in mission_modes and previous_mode in mission_modes:
            return False, "Mission continuing"
        
        # Case 6: From LOITER but not after RTL (waypoint reached, holding)
        if (current_mode in mission_modes and 
            previous_mode == 'AUTO.LOITER' and
            not self.rtl_completed):
            return False, "Mission continuing from waypoint hold"
        
        return False, ""

    def status_callback(self, msg):
        with self.data_lock:
            previous_arming = self.vehicle_status.arming_state if self.vehicle_status else None
            previous_mode = self.get_current_mode() if self.vehicle_status else None
            
            self.vehicle_status = msg
            current_mode = self.get_current_mode()
            
            # Track arming
            if previous_arming != msg.arming_state:
                if msg.arming_state == 2:  # ARMED
                    self.armed_time = time.time()
                    self.emergency_triggered = False
                    self.emergency_trigger_source = None
                    self.consecutive_gps_failures = 0
                    for sensor in self.sensor_failure_counts:
                        self.sensor_failure_counts[sensor] = 0
                    self.get_logger().info('✈️ Armed - Fault detector monitoring sensors')
                else:
                    self.armed_time = None
                    self.mission_start_time = None
                    self.get_logger().info('🛑 Disarmed')
            
            # Track mode changes
            if previous_mode != current_mode:
                # Unified mission start detection
                is_new_mission, reason = self.detect_mission_start(current_mode, previous_mode)
                if is_new_mission:
                    self.mission_start_time = time.time()
                    self.mission_count += 1
                    self.get_logger().info(
                        f'🚀 Mission #{self.mission_count} started ({reason}) - '
                        f'Grace period: {self.mission_start_grace}s'
                    )
                
                # Track RTL completion
                if previous_mode == 'AUTO.RTL' and current_mode == 'AUTO.LOITER':
                    self.rtl_completed = True
                    self.get_logger().info('✈️ RTL completed - loitering at home')
                
                # Log if entering RTL/LAND but we didn't trigger it
                if current_mode in ['AUTO.RTL', 'AUTO.LAND'] and not self.emergency_triggered:
                    self.get_logger().info(f'ℹ️ {current_mode} detected (triggered by PX4 or other node)')
                
                # Mission pause detection
                if previous_mode in ['AUTO.MISSION'] and current_mode == 'AUTO.LOITER':
                    if not self.emergency_triggered:  # Not emergency related
                        self.get_logger().info('⏸️ Mission paused - Loitering at waypoint')

    def command_ack_callback(self, msg):
        with self.data_lock:
            if self.pending_command_timestamp is not None:
                time_since_command = time.time() - self.last_emergency_command_time
                
                if msg.command == VehicleCommand.VEHICLE_CMD_NAV_RETURN_TO_LAUNCH and time_since_command < 2.0:
                    if msg.result == VehicleCommandAck.VEHICLE_CMD_RESULT_ACCEPTED:
                        self.get_logger().info('✅ Fault detector RTL command accepted')
                        self.emergency_triggered = True
                        self.pending_command_timestamp = None
                    else:
                        self.get_logger().error(f'❌ Fault detector RTL command failed: {msg.result}')
                        self.pending_command_timestamp = None

    def is_failsafe_active(self):
        if self.vehicle_status:
            return self.vehicle_status.failsafe
        return False

    def get_flight_time(self):
        if self.armed_time is None:
            return 0
        return time.time() - self.armed_time

    def is_in_grace_period(self):
        """Check if in any grace period"""
        current_time = time.time()
        
        # Startup grace
        if (current_time - self.node_start_time) < self.startup_grace:
            return True
        
        # Mission start grace
        if self.mission_start_time and (current_time - self.mission_start_time) < self.mission_start_grace:
            return True
        
        # Min flight time
        if self.get_flight_time() < self.min_flight_time:
            return True
            
        return False

    def check_sensor_health(self, current_time):
        """Check health of all sensors"""
        health_status = {}
        
        # GPS health check (critical for both SITL and real)
        if self.sensor_data['gps'] is None:
            # In SITL, GPS might take time to initialize
            health_status['gps'] = self.sitl_mode and (current_time - self.node_start_time) < 60
        else:
            gps = self.sensor_data['gps']
            
            # Adjust requirements for SITL
            min_sats = 6 if self.sitl_mode else self.min_satellites
            min_fix = 2 if self.sitl_mode else self.min_fix_type
            
            gps_ok = (gps.fix_type >= min_fix and
                     gps.satellites_used >= min_sats)
            
            # Check for timeout (more lenient in SITL)
            timeout = self.gps_timeout * 2 if self.sitl_mode else self.gps_timeout
            if self.sensor_timestamps['gps']:
                if (current_time - self.sensor_timestamps['gps']) > timeout:
                    gps_ok = False
            
            # Check jamming/spoofing (only in real craft)
            if not self.sitl_mode:
                if hasattr(gps, 'jamming_state') and gps.jamming_state >= 2:
                    gps_ok = False
                    self.get_logger().warn('⚠️ GPS jamming detected!')
                
                if hasattr(gps, 'spoofing_state') and gps.spoofing_state >= 2:
                    gps_ok = False
                    self.get_logger().warn('⚠️ GPS spoofing detected!')
            
            health_status['gps'] = gps_ok
        
        # Additional sensor checks (mainly for real craft)
        if self.enable_sensor_checks and not self.sitl_mode:
            # IMU
            if self.sensor_timestamps['imu']:
                health_status['imu'] = (current_time - self.sensor_timestamps['imu']) < self.imu_timeout
            else:
                health_status['imu'] = True  # Don't fail if not receiving
            
            # Magnetometer
            if self.sensor_timestamps['mag']:
                health_status['mag'] = (current_time - self.sensor_timestamps['mag']) < self.mag_timeout
            else:
                health_status['mag'] = True
            
            # Barometer
            if self.sensor_timestamps['baro']:
                health_status['baro'] = (current_time - self.sensor_timestamps['baro']) < self.baro_timeout
            else:
                health_status['baro'] = True
            
            # RC (optional in autonomous)
            if self.sensor_data['rc'] and self.sensor_timestamps['rc']:
                rc_ok = (current_time - self.sensor_timestamps['rc']) < self.rc_timeout
                if hasattr(self.sensor_data['rc'], 'rssi') and self.sensor_data['rc'].rssi < self.min_rc_quality:
                    rc_ok = False
                health_status['rc'] = rc_ok
            else:
                health_status['rc'] = True  # Don't fail on missing RC
        elif self.sitl_mode:
            # In SITL, assume sensors are healthy if not monitored
            health_status['imu'] = True
            health_status['mag'] = True
            health_status['baro'] = True
            health_status['rc'] = True
        
        # Estimator (important for both SITL and real)
        if self.sensor_timestamps['estimator']:
            timeout = self.estimator_timeout * 2 if self.sitl_mode else self.estimator_timeout
            health_status['estimator'] = (current_time - self.sensor_timestamps['estimator']) < timeout
        else:
            # More lenient in SITL during startup
            health_status['estimator'] = self.sitl_mode and (current_time - self.node_start_time) < 60
        
        # Update sensor health status
        for sensor, is_healthy in health_status.items():
            if sensor in self.sensor_health:
                self.sensor_health[sensor] = is_healthy
                if not is_healthy:
                    self.sensor_failure_counts[sensor] += 1
                else:
                    self.sensor_failure_counts[sensor] = 0
        
        return health_status

    def send_emergency_rtl_command(self):
        """Send emergency RTL command"""
        current_time = time.time()
        
        # Check cooldown
        if current_time - self.last_emergency_command_time < self.command_cooldown:
            return False
        
        self.pending_command_timestamp = int(current_time * 1000)
        
        msg = VehicleCommand()
        msg.timestamp = int(self.get_clock().now().nanoseconds / 1000)
        msg.command = VehicleCommand.VEHICLE_CMD_NAV_RETURN_TO_LAUNCH
        msg.param1 = 0.0
        msg.param2 = 0.0
        msg.param3 = 0.0
        msg.param4 = 0.0
        msg.param5 = 0.0
        msg.param6 = 0.0
        msg.param7 = 0.0
        msg.target_system = 1
        msg.target_component = 1
        msg.source_system = 1
        msg.source_component = 1
        msg.from_external = True
        
        self.vehicle_command_pub.publish(msg)
        self.last_emergency_command_time = current_time
        return True

    def check_system_health(self):
        """Main health check logic"""
        with self.data_lock:
            # Skip if already in emergency
            current_mode = self.get_current_mode()
            if current_mode in ['AUTO.LAND', 'AUTO.RTL']:
                return
            
            # Skip if not armed
            if self.armed_time is None:
                return
            
            # Skip if in grace period
            if self.is_in_grace_period():
                return
            
            # Skip if PX4 failsafe active
            if self.is_failsafe_active():
                self.get_logger().debug('PX4 failsafe active - fault detector standing by')
                return
            
            current_time = time.time()
            
            # Check all sensor health
            health_status = self.check_sensor_health(current_time)
            
            # Log status periodically
            if current_time - self.last_health_log_time > 10:
                unhealthy = [s for s, h in health_status.items() if not h]
                if unhealthy:
                    self.get_logger().warn(f'⚠️ Unhealthy sensors: {", ".join(unhealthy)}')
                else:
                    self.get_logger().info('✅ All monitored sensors healthy')
                self.last_health_log_time = current_time
            
            # Check for critical GPS failure
            if not health_status.get('gps', True):
                self.consecutive_gps_failures += 1
                
                # Adjust threshold for SITL
                failure_threshold = self.gps_failure_threshold * 2 if self.sitl_mode else self.gps_failure_threshold
                
                if self.consecutive_gps_failures >= failure_threshold:
                    # Check altitude before triggering
                    if self.current_altitude < self.min_altitude:
                        self.get_logger().warn(f'GPS critical but altitude too low ({self.current_altitude:.1f}m)')
                        return
                    
                    # Determine reason
                    if self.sensor_data['gps'] is None:
                        reason = "No GPS data"
                    elif self.sensor_data['gps'].fix_type < (2 if self.sitl_mode else self.min_fix_type):
                        reason = f"GPS fix lost (type={self.sensor_data['gps'].fix_type})"
                    elif self.sensor_data['gps'].satellites_used < (6 if self.sitl_mode else self.min_satellites):
                        reason = f"Low satellites ({self.sensor_data['gps'].satellites_used})"
                    else:
                        reason = "GPS timeout"
                    
                    # Only trigger RTL in real craft or severe SITL failures
                    if not self.sitl_mode or self.consecutive_gps_failures >= failure_threshold * 2:
                        self.trigger_emergency_rtl(f"GPS failure: {reason}")
                    else:
                        self.get_logger().warn(f'GPS degraded in SITL: {reason} ({self.consecutive_gps_failures}/{failure_threshold*2})')
            else:
                self.consecutive_gps_failures = 0
            
            # Check for critical estimator failure (less strict in SITL)
            if not health_status.get('estimator', True):
                if self.sitl_mode:
                    # In SITL, only warn unless it's been down for a while
                    if self.sensor_failure_counts['estimator'] > 10:
                        self.get_logger().error('Estimator failure in SITL')
                        self.trigger_emergency_rtl("Estimator failure")
                else:
                    # In real craft, act faster
                    if self.sensor_failure_counts['estimator'] > 3:
                        self.trigger_emergency_rtl("Estimator failure")

    def trigger_emergency_rtl(self, reason):
        """Trigger emergency RTL"""
        if self.emergency_triggered:
            return
        
        self.get_logger().error(f'🚨 FAULT DETECTOR triggering RTL: {reason}')
        self.emergency_trigger_source = reason
        
        if self.send_emergency_rtl_command():
            self.get_logger().info('📡 Fault detector RTL command sent')

    def publish_status(self):
        """Publish comprehensive fault detector status"""
        with self.data_lock:
            status = {
                'timestamp': time.time(),
                'armed': self.armed_time is not None,
                'mode': self.get_current_mode(),
                'mission_count': self.mission_count,
                'sensors': self.sensor_health,
                'failure_counts': self.sensor_failure_counts,
                'gps_failures': self.consecutive_gps_failures,
                'emergency_triggered': self.emergency_triggered,
                'trigger_source': self.emergency_trigger_source,
                'altitude': self.current_altitude,
                'flight_time': self.get_flight_time(),
                'rtl_completed': self.rtl_completed
            }
            
            msg = String()
            msg.data = json.dumps(status)
            self.fault_status_pub.publish(msg)

def main(args=None):
    rclpy.init(args=args)
    node = FaultDetector()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        node.get_logger().info('Shutting down Fault Detector')
    finally:
        node.destroy_node()
        rclpy.shutdown()

if __name__ == '__main__':
    main()
