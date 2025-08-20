#!/usr/bin/env python3
"""
POWER MONITOR NODE - FINAL VERSION
"""

import rclpy
from rclpy.node import Node
from rclpy.qos import QoSProfile, ReliabilityPolicy, HistoryPolicy, DurabilityPolicy
from px4_msgs.msg import BatteryStatus, VehicleStatus, VehicleCommand, VehicleCommandAck, VehicleGlobalPosition
from std_msgs.msg import String
from threading import Lock
from collections import deque
import time
import math
import json

class PowerMonitorMultiMission(Node):
    def __init__(self):
        super().__init__('power_monitor_multi_mission')
        
        # Complete Parameters Set
        self.declare_parameter('rtl_battery_remaining', 30.0)
        self.declare_parameter('battery_check_interval', 5.0)
        self.declare_parameter('mission_start_grace_period', 30.0)
        self.declare_parameter('min_flight_time_before_rtl', 20.0)
        self.declare_parameter('home_position_timeout', 30.0)
        
        # Advanced parameters
        self.declare_parameter('safety_margin', 0.4)  # 40% safety margin for return
        self.declare_parameter('average_return_speed', 15.0)  # m/s cruise speed
        self.declare_parameter('battery_capacity_mah', 2000.0)
        self.declare_parameter('min_current_threshold', 0.1)
        self.declare_parameter('current_averaging_window', 10)
        self.declare_parameter('loiter_radius', 80.0)  # Fixed-wing loiter radius
        self.declare_parameter('rtl_reset_timeout', 60.0)
        self.declare_parameter('command_cooldown', 5.0)
        self.declare_parameter('max_command_retries', 3)
        
        # WIG Effect Parameters (for logging only)
        self.declare_parameter('wing_span', 5.5)  # Aircraft wingspan in meters
        self.declare_parameter('wig_height_threshold', 11.0)  # Track WIG below h/b = 2.0
        self.declare_parameter('min_safe_altitude', 1.5)  # Minimum safe altitude
        
        self.declare_parameter('sitl_mode', True)
        self.declare_parameter('enable_rtl_trigger', True)
        
        # Get all parameters
        self.rtl_battery_remaining = self.get_parameter('rtl_battery_remaining').value
        self.check_interval = self.get_parameter('battery_check_interval').value
        self.mission_start_grace = self.get_parameter('mission_start_grace_period').value
        self.min_flight_time = self.get_parameter('min_flight_time_before_rtl').value
        self.home_timeout = self.get_parameter('home_position_timeout').value
        self.safety_margin = self.get_parameter('safety_margin').value
        self.return_speed = self.get_parameter('average_return_speed').value
        self.battery_capacity = self.get_parameter('battery_capacity_mah').value
        self.min_current = self.get_parameter('min_current_threshold').value
        self.current_window_size = self.get_parameter('current_averaging_window').value
        self.loiter_radius = self.get_parameter('loiter_radius').value
        self.rtl_reset_timeout = self.get_parameter('rtl_reset_timeout').value
        self.command_cooldown = self.get_parameter('command_cooldown').value
        self.max_retries = self.get_parameter('max_command_retries').value
        self.wing_span = self.get_parameter('wing_span').value
        self.wig_height_threshold = self.get_parameter('wig_height_threshold').value
        self.min_safe_alt = self.get_parameter('min_safe_altitude').value
        self.sitl_mode = self.get_parameter('sitl_mode').value
        self.enable_rtl = self.get_parameter('enable_rtl_trigger').value
        
        # Complete State Management
        self.battery_status = None
        self.vehicle_status = None
        self.global_position = None
        self.home_position = None
        self.rtl_triggered = False
        self.rtl_trigger_time = None
        self.rtl_completed = False
        self.rtl_triggered_by_monitor = False  # Track if we triggered RTL
        self.lock = Lock()
        self.node_start_time = time.time()
        self.mission_start_time = None
        self.armed_time = None
        self.last_battery_log_time = 0
        self.battery_percentage = None
        self.last_valid_battery = None
        
        # Multi-mission tracking
        self.session_start_time = None  # First arm in session
        self.total_session_flight_time = 0  # Cumulative flight time
        self.mission_count = 0  # Number of missions in session
        self.last_disarm_time = None
        self.session_battery_used = 0  # Track total battery used in session
        self.initial_session_battery = None  # Battery at session start
        
        # Current monitoring (persists across missions in same session)
        self.current_history = deque(maxlen=self.current_window_size)
        self.sitl_default_current = 15.0
        
        # Command tracking
        self.last_command_time = 0
        self.pending_rtl_command = False
        self.command_retry_count = 0
        
        # WIG effect tracking (cumulative across missions)
        self.in_wig_effect = False
        self.wig_time_total = 0  # Total across all missions
        self.wig_time_start = None
        self.wig_efficiency_log = []
        self.current_h_over_b = None
        self.current_wig_efficiency = 0.0
        self.session_wig_time = 0  # Total WIG time in session
        
        # Mode tracking for debugging
        self.mode_history = deque(maxlen=10)
        self.previous_mode = None
        
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
        
        self.status_pub = self.create_publisher(
            String,
            '/power_monitor/status',
            10
        )
        
        # Subscribers
        self.create_subscription(BatteryStatus, '/fmu/out/battery_status', 
                                self.battery_callback, qos_profile=px4_qos_profile)
        self.create_subscription(VehicleStatus, '/fmu/out/vehicle_status', 
                                self.status_callback, qos_profile=px4_qos_profile)
        self.create_subscription(VehicleCommandAck, '/fmu/out/vehicle_command_ack', 
                                self.command_ack_callback, qos_profile=px4_qos_profile)
        self.create_subscription(VehicleGlobalPosition, '/fmu/out/vehicle_global_position',
                                self.gps_callback, qos_profile=px4_qos_profile)
        
        # Main timer
        self.create_timer(self.check_interval, self.check_battery_status)
        
        # Status publisher timer (1Hz)
        self.create_timer(1.0, self.publish_status)
        
        # Startup message
        self.print_startup_info()
    
    def print_startup_info(self):
        """Print comprehensive startup information"""
        self.get_logger().info('='*60)
        self.get_logger().info('POWER MONITOR - MULTI-MISSION VERSION')
        self.get_logger().info('='*60)
        self.get_logger().info(f'✈️ Fixed-Wing Configuration:')
        self.get_logger().info(f'  • RTL Battery Threshold: {self.rtl_battery_remaining:.1f}%')
        self.get_logger().info(f'  • Return Speed: {self.return_speed:.1f} m/s')
        self.get_logger().info(f'  • Safety Margin: {self.safety_margin*100:.0f}%')
        self.get_logger().info(f'  • Loiter Radius: {self.loiter_radius:.0f}m')
        self.get_logger().info(f'🔄 Multi-Mission Support:')
        self.get_logger().info(f'  • Tracks cumulative session statistics')
        self.get_logger().info(f'  • Updates home position for each mission')
        self.get_logger().info(f'  • Maintains battery history across land/takeoff')
        self.get_logger().info(f'🌊 WIG Effect Parameters (INFORMATION ONLY):')
        self.get_logger().info(f'  • Wing Span: {self.wing_span:.1f}m')
        self.get_logger().info(f'  • WIG Zone: h/b < 2.0 (below {self.wig_height_threshold:.1f}m)')
        self.get_logger().info(f'  • Max Efficiency: +35% at h/b ≈ 0.1')
        self.get_logger().info(f'  • Model: K/(1+(h/b)^n) where K=0.35, n=2.5')
        self.get_logger().info(f'  ⚠️ NOTE: WIG efficiency is NOT used in RTL calculations')
        self.get_logger().info(f'  ⚠️ All battery/RTL decisions are conservative (no WIG)')
        self.get_logger().info(f'⏱️ Grace Periods:')
        self.get_logger().info(f'  • Mission Start: {self.mission_start_grace:.0f}s')
        self.get_logger().info(f'  • Min Flight Time: {self.min_flight_time:.0f}s')
        self.get_logger().info('='*60).get_logger().info(f'  • Updates home position for each mission')
        self.get_logger().info(f'  • Maintains battery history across land/takeoff')
        self.get_logger().info(f'🌊 WIG Effect Parameters (Logging Only):')
        self.get_logger().info(f'  • Wing Span: {self.wing_span:.1f}m')
        self.get_logger().info(f'  • WIG Tracking Below: {self.wig_height_threshold:.1f}m')
        self.get_logger().info(f'  • Min Safe Altitude: {self.min_safe_alt:.1f}m')
        self.get_logger().info(f'⏱️ Grace Periods:')
        self.get_logger().info(f'  • Mission Start: {self.mission_start_grace:.0f}s')
        self.get_logger().info(f'  • Min Flight Time: {self.min_flight_time:.0f}s')
        self.get_logger().info('='*60)
    
    def get_px4_mode_name(self, nav_state):
        """Get mode name from PX4 main nav_state value"""
        # Updated mapping based on PX4 main VehicleStatus.msg
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
    
    def calculate_wig_efficiency(self, altitude_agl):
        """
        Calculate WIG efficiency - for LOGGING/INFORMATION ONLY
        
        THIS IS NOT USED IN ANY BATTERY OR RTL CALCULATIONS!
        Only for pilot information and post-flight analysis.
        """
        if altitude_agl is None or altitude_agl < 0:
            return 0.0
        
        h_over_b = altitude_agl / self.wing_span
        
        # Hyperbolic model parameters
        K = 0.35  # Maximum 35% efficiency gain
        p = 0.9979433 
        q = 2.7269718
        
        if h_over_b < 2.0: 
            efficiency_gain = K / ((1 + (h_over_b ** p))**q)
            return efficiency_gain
        else:
            return 0.0
    
    def battery_callback(self, msg):
        """Process battery with session tracking"""
        with self.lock:
            self.battery_status = msg
            
            # Get battery percentage
            if hasattr(msg, 'remaining'):
                new_percentage = None
                
                if 0 <= msg.remaining <= 1.0:
                    new_percentage = msg.remaining * 100.0
                elif 1.0 < msg.remaining <= 100.0:
                    new_percentage = msg.remaining
                
                if new_percentage is not None and 0 <= new_percentage <= 100:
                    # Track battery usage across session
                    if self.battery_percentage is not None and self.session_start_time is not None:
                        battery_delta = self.battery_percentage - new_percentage
                        if battery_delta > 0:
                            self.session_battery_used += battery_delta
                    
                    self.battery_percentage = new_percentage
                    self.last_valid_battery = new_percentage
                    
                    # Set initial session battery
                    if self.initial_session_battery is None and self.is_armed():
                        self.initial_session_battery = new_percentage
                        
                elif self.last_valid_battery is not None:
                    self.battery_percentage = self.last_valid_battery
            
            # Track current
            if hasattr(msg, 'current_a'):
                current = msg.current_a if msg.current_a > 0 else self.sitl_default_current
                self.current_history.append(current)
    
    def gps_callback(self, msg):
        """Process GPS with WIG effect detection"""
        with self.lock:
            self.global_position = msg
            
            # Set home position only once on first valid GPS after arming
            if (self.home_position is None and 
                self.is_armed() and
                msg.lat != 0 and msg.lon != 0):
                
                self.home_position = (msg.lat, msg.lon, msg.alt)
                self.get_logger().info(
                    f'🏠 Home position set: '
                    f'lat={msg.lat:.6f}, lon={msg.lon:.6f}, alt={msg.alt:.2f}m'
                )
            
            # WIG effect detection (cumulative tracking)
            if self.is_armed() and hasattr(msg, 'alt'):
                altitude_agl = self.get_altitude_agl()
                
                if altitude_agl is not None:
                    self.current_h_over_b = altitude_agl / self.wing_span
                    self.current_wig_efficiency = self.calculate_wig_efficiency(altitude_agl)
                    
                    was_in_wig = self.in_wig_effect
                    self.in_wig_effect = (altitude_agl <= self.wig_height_threshold and 
                                         altitude_agl >= self.min_safe_alt and
                                         self.current_wig_efficiency > 0.05)
                    
                    # Track WIG transitions
                    if self.in_wig_effect and not was_in_wig:
                        self.wig_time_start = time.time()
                        self.get_logger().info(
                            f'🌊 Entered WIG effect zone - Alt: {altitude_agl:.1f}m, '
                            f'h/b: {self.current_h_over_b:.2f}, '
                            f'Efficiency: +{self.current_wig_efficiency*100:.1f}% '
                            f'(INFO ONLY - not used in calculations)'
                        )
                    elif not self.in_wig_effect and was_in_wig:
                        if self.wig_time_start:
                            session_time = time.time() - self.wig_time_start
                            self.wig_time_total += session_time
                            self.session_wig_time += session_time
                            if self.wig_efficiency_log:
                                avg_eff = sum(self.wig_efficiency_log) / len(self.wig_efficiency_log)
                                self.get_logger().info(
                                    f'☁️ Exited WIG zone - Duration: {session_time:.1f}s, '
                                    f'Avg efficiency: +{avg_eff*100:.1f}% '
                                    f'(INFO ONLY - not affecting battery calcs)'
                                )
                        self.wig_efficiency_log = []
                    
                    if self.in_wig_effect:
                        self.wig_efficiency_log.append(self.current_wig_efficiency)
    
    def status_callback(self, msg):
        """Process vehicle status with multi-mission tracking"""
        with self.lock:
            previous_mode = self.get_current_mode() if self.vehicle_status else None
            previous_arming = self.vehicle_status.arming_state if self.vehicle_status else None
            previous_failsafe = self.vehicle_status.failsafe if self.vehicle_status else False
            
            self.vehicle_status = msg
            current_mode = self.get_current_mode()
            
            # Track mode changes
            if previous_mode != current_mode:
                self.mode_history.append((time.time(), previous_mode, current_mode))
                self.get_logger().info(f'✈️ Mode: {previous_mode} → {current_mode}')
                
                # Detect RTL triggered by PX4 (not by power monitor)
                if current_mode == 'AUTO.RTL' and not self.rtl_triggered:
                    # RTL entered but we didn't trigger it - PX4 must have triggered it
                    failsafe_reason = self.get_failsafe_reason()
                    self.get_logger().warn(
                        f'⚠️ PX4 TRIGGERED RTL - Reason: {failsafe_reason}'
                    )
                    self.get_logger().warn(
                        f'📋 PX4 is handling failsafe - Power monitor standing by'
                    )
                    # Mark as triggered so we don't try to trigger it again
                    self.rtl_triggered = True
                    self.rtl_trigger_time = time.time()
                
                # Mission start detection
                if current_mode in ['AUTO.MISSION', 'AUTO.TAKEOFF'] and \
                   previous_mode not in ['AUTO.MISSION', 'AUTO.TAKEOFF', 'AUTO.LOITER']:
                    self.mission_start_time = time.time()
                    self.mission_count += 1
                    self.get_logger().info(
                        f'🚀 Mission #{self.mission_count} started - '
                        f'Grace period: {self.mission_start_grace}s'
                    )
                
                # RTL completion
                if previous_mode == 'AUTO.RTL':
                    if current_mode == 'AUTO.LOITER':
                        self.handle_rtl_completion('LOITER')
                    elif current_mode in ['AUTO.LAND', 'MANUAL']:
                        self.handle_rtl_completion('LAND')
                
                # Mission resume
                if self.rtl_triggered and current_mode in ['AUTO.MISSION']:
                    self.handle_mission_resume()
            
            # Detect failsafe state changes
            if previous_failsafe != msg.failsafe:
                if msg.failsafe:
                    self.get_logger().warn(f'🚨 PX4 FAILSAFE ACTIVATED - {self.get_failsafe_reason()}')
                else:
                    self.get_logger().info(f'✅ PX4 failsafe cleared')
            
            # Track arming/disarming
            if previous_arming != msg.arming_state:
                if msg.arming_state == 2:  # ARMED
                    self.handle_arm()
                elif msg.arming_state == 1:  # DISARMED
                    self.handle_disarm()
            
            self.previous_mode = current_mode
    
    def command_ack_callback(self, msg):
        """Process command acknowledgments"""
        with self.lock:
            if self.pending_rtl_command:
                if msg.command == VehicleCommand.VEHICLE_CMD_NAV_RETURN_TO_LAUNCH:
                    if msg.result == VehicleCommandAck.VEHICLE_CMD_RESULT_ACCEPTED:
                        self.get_logger().info('✅ RTL command ACCEPTED (Power Monitor triggered)')
                        self.rtl_triggered = True
                        self.rtl_triggered_by_monitor = True  # We triggered it
                        self.rtl_trigger_time = time.time()
                        self.pending_rtl_command = False
                        self.command_retry_count = 0
                    else:
                        self.get_logger().warn(f'⚠️ RTL command result: {msg.result}')
                        self.command_retry_count += 1
                        self.pending_rtl_command = False
    
    def get_current_mode(self):
        """Get current flight mode using correct PX4 mapping"""
        if not self.vehicle_status:
            return "UNKNOWN"
        
        return self.get_px4_mode_name(self.vehicle_status.nav_state)
    
    def is_armed(self):
        if self.vehicle_status:
            return self.vehicle_status.arming_state == 2
        return False
    
    def is_failsafe_active(self):
        if self.vehicle_status:
            return self.vehicle_status.failsafe
        return False
    
    def get_failsafe_reason(self):
        """Determine the likely reason for failsafe/RTL"""
        if not self.vehicle_status:
            return "Unknown"
        
        reasons = []
        
        # Check failsafe flag
        if self.vehicle_status.failsafe:
            reasons.append("Failsafe active")
        
        # Check failure detector status if available
        if hasattr(self.vehicle_status, 'failure_detector_status'):
            failures = []
            status = self.vehicle_status.failure_detector_status
            
            # Check each failure bit
            if status & 1:  # FAILURE_ROLL
                failures.append("Roll")
            if status & 2:  # FAILURE_PITCH
                failures.append("Pitch") 
            if status & 4:  # FAILURE_ALT
                failures.append("Altitude")
            if status & 8:  # FAILURE_EXT
                failures.append("External")
            if status & 16:  # FAILURE_ARM_ESC
                failures.append("ESC")
            if status & 32:  # FAILURE_BATTERY
                failures.append("Battery")
            if status & 64:  # FAILURE_IMBALANCED_PROP
                failures.append("Prop imbalance")
            if status & 128:  # FAILURE_MOTOR
                failures.append("Motor")
            
            if failures:
                reasons.append(f"Failures: {', '.join(failures)}")
        
        # Check GCS connection
        if hasattr(self.vehicle_status, 'gcs_connection_lost'):
            if self.vehicle_status.gcs_connection_lost:
                reasons.append("GCS connection lost")
        
        # Check if it could be manual switch or GCS command
        if not reasons and not self.rtl_triggered:
            # If no specific failure but RTL was entered, likely manual or GCS
            reasons.append("Manual switch or GCS command")
        
        return " | ".join(reasons) if reasons else "Unknown reason"
    
    def get_flight_time(self):
        """Get current mission flight time"""
        if self.armed_time is None:
            return 0
        return time.time() - self.armed_time
    
    def get_session_flight_time(self):
        """Get total session flight time"""
        current_flight = self.get_flight_time() if self.is_armed() else 0
        return self.total_session_flight_time + current_flight
    
    def get_altitude_agl(self):
        """Get altitude above ground level"""
        if self.global_position and self.home_position:
            return self.global_position.alt - self.home_position[2]
        return None
    
    def is_in_grace_period(self):
        """Check if in any grace period"""
        current_time = time.time()
        
        # Mission start grace
        if self.mission_start_time is not None:
            time_since_mission = current_time - self.mission_start_time
            if time_since_mission < self.mission_start_grace:
                remaining = self.mission_start_grace - time_since_mission
                self.get_logger().info(f'⏳ Mission grace: {remaining:.1f}s remaining')
                return True
        
        # Min flight time
        flight_time = self.get_flight_time()
        if flight_time > 0 and flight_time < self.min_flight_time:
            remaining = self.min_flight_time - flight_time
            self.get_logger().info(f'⏳ Min flight time: {remaining:.1f}s remaining')
            return True
            
        return False
    
    def calculate_distance_to_home(self):
        """Calculate distance to home"""
        if self.home_position is None or self.global_position is None:
            return None
        
        lat1, lon1, alt1 = self.home_position
        lat2 = self.global_position.lat
        lon2 = self.global_position.lon
        alt2 = self.global_position.alt
        
        # Haversine formula
        R = 6371000
        phi1 = math.radians(lat1)
        phi2 = math.radians(lat2)
        delta_phi = math.radians(lat2 - lat1)
        delta_lambda = math.radians(lon2 - lon1)
        
        a = math.sin(delta_phi/2)**2 + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda/2)**2
        c = 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))
        
        horizontal = R * c
        vertical = abs(alt2 - alt1) * 0.3
        
        return horizontal + 0.5*vertical
    
    def get_average_current(self):
        """Get average current draw"""
        if not self.current_history:
            return self.sitl_default_current if self.sitl_mode else None
        return sum(self.current_history) / len(self.current_history)
    
    def calculate_remaining_time(self):
        """
        Calculate battery time remaining - CONSERVATIVE CALCULATION
        
        IMPORTANT: This does NOT include WIG efficiency gains!
        WIG effect is tracked for information only but never used 
        in RTL decisions for safety reasons.
        """
        if self.battery_percentage is None:
            return None
        
        avg_current = self.get_average_current()
        if avg_current is None or avg_current < self.min_current:
            return None
        
        # Get battery capacity
        capacity = self.battery_capacity
        if hasattr(self.battery_status, 'capacity') and self.battery_status.capacity > 0:
            capacity = self.battery_status.capacity
        
        # Calculate remaining time WITHOUT any efficiency gains
        # This ensures conservative RTL decisions for safety
        remaining_mah = (self.battery_percentage / 100.0) * capacity
        current_draw_ma = avg_current * 1000.0  # No efficiency factor applied!
        
        if current_draw_ma > 0:
            time_hours = remaining_mah / current_draw_ma
            return time_hours * 3600.0
        return None
    
    def calculate_return_time(self):
        """Calculate time needed to return"""
        distance = self.calculate_distance_to_home()
        if distance is None:
            return None
        
        base_time = distance / self.return_speed
        loiter_time = (2 * math.pi * self.loiter_radius) / self.return_speed
        total_time = (base_time + loiter_time) * (1 + self.safety_margin)
        
        return total_time + 120  # 2 minutes extra
    
    def send_rtl_command(self):
        """Send RTL command"""
        if time.time() - self.last_command_time < self.command_cooldown:
            return False
        
        self.get_logger().warn('🚨 POWER MONITOR SENDING RTL COMMAND (Low Battery/Time)')
        
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
        self.pending_rtl_command = True
        self.last_command_time = time.time()
        return True
    
    def handle_arm(self):
        """Handle arming event"""
        self.armed_time = time.time()
        
        # Track session start
        if self.session_start_time is None:
            self.session_start_time = time.time()
            self.get_logger().info('📊 New flight session started')
        elif self.last_disarm_time:
            ground_time = time.time() - self.last_disarm_time
            self.get_logger().info(f'✈️ Re-armed after {ground_time/60:.1f} minutes on ground')
        
        self.reset_rtl_state("Vehicle armed")
        self.get_logger().info(f'✈️ ARMED - Mission #{self.mission_count + 1} ready')
    
    def handle_disarm(self):
        """Handle disarm event"""
        # Update session statistics
        if self.armed_time:
            flight_time = time.time() - self.armed_time
            self.total_session_flight_time += flight_time
            self.get_logger().info(f'⏱️ Flight time: {flight_time/60:.1f} min')
        
        # Log session statistics
        if self.mission_count > 0:
            self.get_logger().info('='*50)
            self.get_logger().info(f'📊 Session Statistics:')
            self.get_logger().info(f'  • Missions completed: {self.mission_count}')
            self.get_logger().info(f'  • Total flight time: {self.total_session_flight_time/60:.1f} min')
            if self.initial_session_battery and self.battery_percentage:
                battery_used = self.initial_session_battery - self.battery_percentage
                self.get_logger().info(f'  • Battery used: {battery_used:.1f}%')
            if self.session_wig_time > 0:
                self.get_logger().info(f'  • WIG time: {self.session_wig_time/60:.1f} min')
            self.get_logger().info('='*50)
        
        # Store disarm time but don't reset session data
        self.last_disarm_time = time.time()
        self.armed_time = None
        self.mission_start_time = None
        
        # Keep home position for entire session - never reset
        # Keep battery history for session continuity
        
        self.rtl_triggered = False
        self.get_logger().info('🛑 DISARMED')
    
    def handle_rtl_completion(self, end_mode):
        """Handle RTL completion"""
        # Determine who triggered RTL
        rtl_source = "Power Monitor" if self.rtl_triggered_by_monitor else "PX4"
        self.get_logger().info(f'✅ RTL completed ({rtl_source} triggered) → {end_mode}')
        self.rtl_completed = True
        
        if end_mode == 'LOITER':
            self.get_logger().info(f'✈️ Loitering at home (radius: {self.loiter_radius}m)')
    
    def handle_mission_resume(self):
        """Handle mission resume after RTL"""
        self.get_logger().info('📍 Mission resumed after RTL')
        self.reset_rtl_state("Mission resumed")
        self.mission_start_time = time.time()
    
    def reset_rtl_state(self, reason):
        """Reset RTL state"""
        if self.rtl_triggered:
            self.get_logger().info(f'🔄 Resetting RTL state: {reason}')
        self.rtl_triggered = False
        self.rtl_triggered_by_monitor = False
        self.rtl_trigger_time = None
        self.rtl_completed = False
        self.command_retry_count = 0
        self.pending_rtl_command = False
    
    def publish_status(self):
        """Publish comprehensive status"""
        with self.lock:
            status = {
                'timestamp': time.time(),
                'battery_pct': self.battery_percentage,
                'current_avg': self.get_average_current(),
                'time_remaining': self.calculate_remaining_time(),
                'distance_home': self.calculate_distance_to_home(),
                'return_time': self.calculate_return_time(),
                'mode': self.get_current_mode(),
                'armed': self.is_armed(),
                'rtl_triggered': self.rtl_triggered,
                'in_wig_effect': self.in_wig_effect,
                'altitude_agl': self.get_altitude_agl(),
                'h_over_b': self.current_h_over_b,
                'wig_efficiency_info': self.current_wig_efficiency,
                'mission_count': self.mission_count,
                'session_flight_time': self.get_session_flight_time(),
                'session_battery_used': self.session_battery_used,
                'session_wig_time': self.session_wig_time
            }
            
            msg = String()
            msg.data = json.dumps(status)
            self.status_pub.publish(msg)
    
    def check_battery_status(self):
        """Main battery checking logic"""
        with self.lock:
            # Skip if not armed
            if not self.is_armed():
                return
            
            # Check if PX4 is handling a failsafe
            current_mode = self.get_current_mode()
            if self.is_failsafe_active():
                # Log once when entering failsafe
                if not getattr(self, '_failsafe_logged', False):
                    self.get_logger().info(f'📋 PX4 failsafe active ({current_mode}) - Power monitor standing by')
                    self._failsafe_logged = True
                return
            else:
                self._failsafe_logged = False
            
            # Skip if RTL already triggered (by us or PX4)
            if self.rtl_triggered:
                if self.rtl_trigger_time:
                    elapsed = time.time() - self.rtl_trigger_time
                    if elapsed > self.rtl_reset_timeout:
                        if current_mode not in ['AUTO.RTL', 'AUTO.LOITER', 'AUTO.LAND']:
                            self.reset_rtl_state(f"RTL timeout ({elapsed:.0f}s)")
                    else:
                        return
                else:
                    return
            
            # Skip if in grace period
            if self.is_in_grace_period():
                return
            
            # Skip if already in safe mode
            if current_mode in ['AUTO.RTL', 'AUTO.LAND', 'MANUAL']:
                # Check if this is PX4-triggered RTL that we haven't logged yet
                if current_mode == 'AUTO.RTL' and not self.rtl_triggered:
                    failsafe_reason = self.get_failsafe_reason()
                    self.get_logger().warn(f'⚠️ Detected PX4-triggered RTL: {failsafe_reason}')
                    self.rtl_triggered = True
                    self.rtl_trigger_time = time.time()
                return
            
            # Check prerequisites
            if self.battery_percentage is None:
                return
            
            if self.home_position is None:
                elapsed = time.time() - self.node_start_time
                if elapsed > self.home_timeout:
                    self.get_logger().warn(f'⚠️ No home position after {self.home_timeout}s')
                return
            
            # Calculate metrics
            distance = self.calculate_distance_to_home()
            remaining_time = self.calculate_remaining_time()
            return_time = self.calculate_return_time()
            altitude = self.get_altitude_agl()
            avg_current = self.get_average_current()
            
            # Build log
            log_parts = [
                f'🔋 Batt: {self.battery_percentage:.1f}%',
                f'📍 Mode: {current_mode}',
                f'#️⃣ Mission: {self.mission_count}'
            ]
            
            if distance:
                log_parts.append(f'🏠 Dist: {distance:.0f}m')
            
            if remaining_time:
                log_parts.append(f'⏱️ Time: {remaining_time/60:.1f}min')
            
            if return_time:
                log_parts.append(f'🔁 RTL: {return_time/60:.1f}min')
            
            if altitude:
                log_parts.append(f'📏 Alt: {altitude:.1f}m')
                if self.in_wig_effect:
                    # Show WIG info but make it clear it's informational only
                    log_parts.append(f'🌊 WIG: +{self.current_wig_efficiency*100:.0f}% (info only)')
            
            self.get_logger().info(' | '.join(log_parts))
            
            # Check RTL conditions
            should_rtl = False
            reason = ""
            
            # Battery threshold
            if self.battery_percentage <= self.rtl_battery_remaining:
                should_rtl = True
                reason = f"battery at threshold ({self.battery_percentage:.1f}%)"
            
            # Time remaining check
            elif remaining_time and return_time and remaining_time < return_time:
                should_rtl = True
                reason = f"insufficient time ({remaining_time/60:.1f}min < {return_time/60:.1f}min)"
            
            # Trigger RTL if needed
            if should_rtl and self.command_retry_count < self.max_retries:
                self.get_logger().warn(f'⚠️ POWER MONITOR RTL NEEDED: {reason}')
                if self.enable_rtl:
                    if self.send_rtl_command():
                        self.command_retry_count += 1
                else:
                    self.get_logger().info('Power Monitor RTL trigger disabled for testing')

def main(args=None):
    rclpy.init(args=args)
    node = PowerMonitorMultiMission()
    
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        node.get_logger().info('Shutting down Power Monitor')
    finally:
        node.destroy_node()
        rclpy.shutdown()

if __name__ == '__main__':
    main()
