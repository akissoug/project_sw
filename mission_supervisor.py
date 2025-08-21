#!/usr/bin/env python3
"""
MISSION SUPERVISOR NODE - IMPROVED VERSION
"""

import rclpy
from rclpy.node import Node
from rclpy.qos import QoSProfile, ReliabilityPolicy, HistoryPolicy, DurabilityPolicy
from px4_msgs.msg import (
    VehicleStatus, VehicleLocalPosition, VehicleCommand, VehicleCommandAck,
    VehicleGlobalPosition, MissionResult, GeofenceResult, FailsafeFlags
)
from std_msgs.msg import String
import time
from threading import Lock
from enum import Enum
import json
import math

class MissionState(Enum):
    IDLE = 0
    ARMED = 1
    TAKEOFF = 2
    MISSION = 3
    WAYPOINT_HOLD = 4
    RTL = 5
    LOITER_HOME = 6
    LANDING = 7
    EMERGENCY = 8
    DISARMED = 9
    PAUSED = 10

class MissionSupervisor(Node):
    def __init__(self):
        super().__init__('mission_supervisor')
        
        # Parameters for mission supervision
        self.declare_parameter('mission_timeout', 5400.0)  # 1.5 hours max mission
        self.declare_parameter('waypoint_timeout', 300.0)  # 5 min per waypoint
        self.declare_parameter('loiter_detection_time', 10.0)
        self.declare_parameter('stuck_detection_radius', 50.0)  # meters
        self.declare_parameter('stuck_detection_time', 60.0)  # seconds
        self.declare_parameter('check_interval', 3.0)
        self.declare_parameter('startup_grace_period', 30.0)
        self.declare_parameter('mission_start_grace_period', 45.0)
        self.declare_parameter('command_cooldown', 15.0)
        self.declare_parameter('min_altitude_for_actions', 20.0)
        self.declare_parameter('max_mission_retries', 2)
        self.declare_parameter('sitl_mode', True)
        self.declare_parameter('enable_geofence_monitoring', True)
        self.declare_parameter('enable_stuck_detection', True)
        
        # Get parameters
        self.mission_timeout = self.get_parameter('mission_timeout').value
        self.waypoint_timeout = self.get_parameter('waypoint_timeout').value
        self.loiter_detection_time = self.get_parameter('loiter_detection_time').value
        self.stuck_radius = self.get_parameter('stuck_detection_radius').value
        self.stuck_time = self.get_parameter('stuck_detection_time').value
        self.check_interval = self.get_parameter('check_interval').value
        self.startup_grace = self.get_parameter('startup_grace_period').value
        self.mission_start_grace = self.get_parameter('mission_start_grace_period').value
        self.command_cooldown = self.get_parameter('command_cooldown').value
        self.min_altitude = self.get_parameter('min_altitude_for_actions').value
        self.max_retries = self.get_parameter('max_mission_retries').value
        self.sitl_mode = self.get_parameter('sitl_mode').value
        self.enable_geofence = self.get_parameter('enable_geofence_monitoring').value
        self.enable_stuck_detection = self.get_parameter('enable_stuck_detection').value
        
        # State variables
        self.mission_state = MissionState.IDLE
        self.vehicle_status = None
        self.local_position = None
        self.global_position = None
        self.mission_result = None
        self.geofence_result = None
        self.failsafe_flags = None
        
        # Mission tracking
        self.mission_start_time = None
        self.armed_time = None
        self.current_waypoint = 0
        self.total_waypoints = 0
        self.waypoint_start_time = None
        self.last_waypoint_change = None
        self.mission_active = False
        self.mission_paused = False
        self.mission_retry_count = 0
        self.rtl_completed = False
        self.rtl_trigger_source = None
        
        # Position tracking for stuck detection
        self.position_history = []
        self.last_position_check = 0
        self.stuck_detected = False
        
        # Mode tracking
        self.last_mode = None
        self.mode_change_time = None
        self.loiter_start_time = None
        
        # Command tracking
        self.last_command_time = 0
        self.pending_command_timestamp = None
        
        # Statistics
        self.mission_stats = {
            'total_missions': 0,
            'successful_missions': 0,
            'failed_missions': 0,
            'total_waypoints': 0,
            'waypoints_reached': 0,
            'geofence_violations': 0,
            'stuck_events': 0
        }
        
        self.data_lock = Lock()
        self.node_start_time = time.time()
        self.current_altitude = 0.0
        self.last_status_log_time = 0
        
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
        
        self.mission_status_pub = self.create_publisher(
            String,
            '/mission_supervisor/status',
            10
        )
        
        # Subscribers
        self.create_subscription(
            VehicleStatus, '/fmu/out/vehicle_status',
            self.vehicle_status_callback, qos_profile=px4_qos_profile
        )
        
        self.create_subscription(
            VehicleLocalPosition, '/fmu/out/vehicle_local_position',
            self.local_position_callback, qos_profile=px4_qos_profile
        )
        
        self.create_subscription(
            VehicleGlobalPosition, '/fmu/out/vehicle_global_position',
            self.global_position_callback, qos_profile=px4_qos_profile
        )
        
        self.create_subscription(
            VehicleCommandAck, '/fmu/out/vehicle_command_ack',
            self.command_ack_callback, qos_profile=px4_qos_profile
        )
        
        self.create_subscription(
            MissionResult, '/fmu/out/mission_result',
            self.mission_result_callback, qos_profile=px4_qos_profile
        )
        
        if self.enable_geofence:
            self.create_subscription(
                GeofenceResult, '/fmu/out/geofence_result',
                self.geofence_callback, qos_profile=px4_qos_profile
            )
        
        self.create_subscription(
            FailsafeFlags, '/fmu/out/failsafe_flags',
            self.failsafe_flags_callback, qos_profile=px4_qos_profile
        )
        
        # Timers
        self.create_timer(self.check_interval, self.supervise_mission)
        self.create_timer(1.0, self.publish_status)
        
        self.print_startup_info()
        
    def print_startup_info(self):
        """Print startup configuration"""
        mode_str = "SITL MODE" if self.sitl_mode else "REAL CRAFT MODE"
        self.get_logger().info('='*60)
        self.get_logger().info(f'MISSION SUPERVISOR - EXECUTION MONITOR ({mode_str})')
        self.get_logger().info('='*60)
        self.get_logger().info(f'📋 Mission Supervision:')
        
        if self.sitl_mode:
            self.get_logger().info(f'  • Mission timeout: {self.mission_timeout*2/60:.1f} min (extended for SITL)')
            self.get_logger().info(f'  • Waypoint timeout: {self.waypoint_timeout*2}s (extended for SITL)')
            if self.enable_stuck_detection:
                self.get_logger().info(f'  • Stuck detection: {self.stuck_radius*2}m radius, {self.stuck_time*1.5}s (relaxed)')
            self.get_logger().info(f'  • Geofence: Warnings only (no RTL in SITL)')
        else:
            self.get_logger().info(f'  • Mission timeout: {self.mission_timeout/60:.1f} min')
            self.get_logger().info(f'  • Waypoint timeout: {self.waypoint_timeout}s')
            if self.enable_stuck_detection:
                self.get_logger().info(f'  • Stuck detection: {self.stuck_radius}m radius, {self.stuck_time}s')
            if self.enable_geofence:
                self.get_logger().info(f'  • Geofence monitoring: Enabled with RTL trigger')
        
        self.get_logger().info(f'  • Max retries: {self.max_retries}')
        self.get_logger().info(f'⏱️ Grace Periods:')
        self.get_logger().info(f'  • Startup: {self.startup_grace}s')
        self.get_logger().info(f'  • Mission start: {self.mission_start_grace}s')
        self.get_logger().info(f'🎯 Focus: Mission execution quality')
        self.get_logger().info(f'  • NOT handling battery (power_monitor\'s job)')
        self.get_logger().info(f'  • NOT handling sensors (fault_detector\'s job)')
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
        
    def vehicle_status_callback(self, msg):
        with self.data_lock:
            previous_arming = self.vehicle_status.arming_state if self.vehicle_status else None
            previous_mode = self.get_current_mode() if self.vehicle_status else None
            
            self.vehicle_status = msg
            current_mode = self.get_current_mode()
            
            # Track arming
            if previous_arming != msg.arming_state:
                if msg.arming_state == 2:  # ARMED
                    self.handle_arm()
                else:
                    self.handle_disarm()
            
            # Track mode changes
            if self.last_mode != current_mode:
                self.get_logger().info(f'✈️ Mode: {self.last_mode} → {current_mode}')
                self.mode_change_time = time.time()
                
                # Track mission start
                if current_mode in ['AUTO.MISSION', 'AUTO.TAKEOFF']:
                    if not self.mission_active:
                        self.handle_mission_start()
                
                # Track LOITER
                if current_mode == 'AUTO.LOITER':
                    self.loiter_start_time = time.time()
                    if previous_mode == 'AUTO.RTL':
                        self.rtl_completed = True
                        self.get_logger().info('✈️ RTL completed - loitering at home')
                    elif previous_mode == 'AUTO.MISSION':
                        self.get_logger().info('⏸️ Mission paused - loitering')
                        self.mission_paused = True
                else:
                    self.loiter_start_time = None
                
                # Track RTL
                if current_mode == 'AUTO.RTL' and previous_mode != 'AUTO.RTL':
                    if not self.rtl_trigger_source:
                        self.rtl_trigger_source = "External (PX4/other node/pilot)"
                    self.get_logger().info(f'ℹ️ RTL detected - triggered by: {self.rtl_trigger_source}')
                
                # Mission resume
                if current_mode == 'AUTO.MISSION' and self.mission_paused:
                    self.get_logger().info('▶️ Mission resumed')
                    self.mission_paused = False
                
                self.last_mode = current_mode
            
    def local_position_callback(self, msg):
        with self.data_lock:
            self.local_position = msg
            if msg.z_valid:
                self.current_altitude = -msg.z  # NED frame
            
            # Track position for stuck detection
            if self.enable_stuck_detection and self.is_armed() and self.mission_active:
                current_time = time.time()
                if current_time - self.last_position_check > 5.0:  # Check every 5s
                    self.position_history.append({
                        'time': current_time,
                        'x': msg.x,
                        'y': msg.y,
                        'z': msg.z
                    })
                    # Keep only last 20 positions (100s of history)
                    if len(self.position_history) > 20:
                        self.position_history.pop(0)
                    self.last_position_check = current_time
                    self.check_if_stuck()
    
    def global_position_callback(self, msg):
        with self.data_lock:
            self.global_position = msg
    
    def mission_result_callback(self, msg):
        with self.data_lock:
            previous_waypoint = self.current_waypoint if self.mission_result else 0
            self.mission_result = msg
            
            # Track waypoint progress
            if hasattr(msg, 'seq_current'):
                self.current_waypoint = msg.seq_current
                if self.current_waypoint != previous_waypoint:
                    self.waypoint_start_time = time.time()
                    self.last_waypoint_change = time.time()
                    self.mission_stats['waypoints_reached'] += 1
                    self.get_logger().info(f'📍 Waypoint {self.current_waypoint}/{self.total_waypoints} reached')
            
            if hasattr(msg, 'seq_total'):
                self.total_waypoints = msg.seq_total
            
            # Check mission completion
            if hasattr(msg, 'finished') and msg.finished:
                if hasattr(msg, 'success') and msg.success:
                    self.handle_mission_success()
                else:
                    self.handle_mission_failure()
    
    def geofence_callback(self, msg):
        with self.data_lock:
            self.geofence_result = msg
            
            # Check for geofence violations
            if hasattr(msg, 'geofence_violated') and msg.geofence_violated:
                self.mission_stats['geofence_violations'] += 1
                
                if self.sitl_mode:
                    # In SITL, just warn but don't trigger RTL
                    self.get_logger().warn('⚠️ GEOFENCE VIOLATION in SITL (not triggering RTL)')
                else:
                    # In real craft, trigger RTL
                    self.get_logger().error('🚫 GEOFENCE VIOLATION DETECTED!')
                    self.trigger_rtl("Geofence violation")
    
    def failsafe_flags_callback(self, msg):
        with self.data_lock:
            self.failsafe_flags = msg
    
    def command_ack_callback(self, msg):
        with self.data_lock:
            if self.pending_command_timestamp is not None:
                time_since_command = time.time() - self.last_command_time
                if time_since_command < 2.0:
                    if msg.command == VehicleCommand.VEHICLE_CMD_NAV_RETURN_TO_LAUNCH:
                        if msg.result == VehicleCommandAck.VEHICLE_CMD_RESULT_ACCEPTED:
                            self.get_logger().info(f'✅ Mission supervisor RTL command accepted')
                        else:
                            self.get_logger().error(f'❌ Mission supervisor RTL command failed: {msg.result}')
                        self.pending_command_timestamp = None
    
    def is_armed(self):
        if not self.vehicle_status:
            return False
        return self.vehicle_status.arming_state == 2
    
    def is_failsafe_active(self):
        if self.vehicle_status:
            return self.vehicle_status.failsafe
        return False
    
    def is_in_grace_period(self):
        """Check if in any grace period"""
        current_time = time.time()
        
        # Startup grace
        if (current_time - self.node_start_time) < self.startup_grace:
            return True
        
        # Mission start grace
        if self.mission_start_time and (current_time - self.mission_start_time) < self.mission_start_grace:
            return True
            
        return False
    
    def handle_arm(self):
        """Handle arming event"""
        self.armed_time = time.time()
        self.position_history = []
        self.stuck_detected = False
        self.rtl_completed = False
        self.rtl_trigger_source = None
        self.get_logger().info('✈️ Armed - Mission supervisor ready')
    
    def handle_disarm(self):
        """Handle disarm event"""
        if self.mission_active:
            # Log mission end statistics
            duration = time.time() - self.mission_start_time if self.mission_start_time else 0
            self.get_logger().info('='*50)
            self.get_logger().info(f'📊 Mission Summary:')
            self.get_logger().info(f'  • Duration: {duration/60:.1f} min')
            self.get_logger().info(f'  • Waypoints: {self.mission_stats["waypoints_reached"]}/{self.total_waypoints}')
            if self.mission_stats['geofence_violations'] > 0:
                self.get_logger().info(f'  • Geofence violations: {self.mission_stats["geofence_violations"]}')
            if self.mission_stats['stuck_events'] > 0:
                self.get_logger().info(f'  • Stuck events: {self.mission_stats["stuck_events"]}')
            self.get_logger().info('='*50)
        
        self.armed_time = None
        self.mission_start_time = None
        self.mission_active = False
        self.mission_paused = False
        self.current_waypoint = 0
        self.total_waypoints = 0
        self.get_logger().info('🛑 Disarmed')
    
    def handle_mission_start(self):
        """Handle mission start"""
        self.mission_start_time = time.time()
        self.mission_active = True
        self.mission_paused = False
        self.waypoint_start_time = time.time()
        self.mission_stats['total_missions'] += 1
        self.mission_retry_count = 0
        self.get_logger().info(f'🚀 Mission #{self.mission_stats["total_missions"]} started')
    
    def handle_mission_success(self):
        """Handle successful mission completion"""
        self.mission_stats['successful_missions'] += 1
        self.get_logger().info('✅ Mission completed successfully!')
        self.mission_active = False
    
    def handle_mission_failure(self):
        """Handle mission failure"""
        self.mission_stats['failed_missions'] += 1
        self.get_logger().warn('⚠️ Mission failed')
        
        if self.mission_retry_count < self.max_retries:
            self.mission_retry_count += 1
            self.get_logger().info(f'🔄 Retrying mission (attempt {self.mission_retry_count}/{self.max_retries})')
        else:
            self.get_logger().error('❌ Max mission retries exceeded')
            self.trigger_rtl("Mission failure - max retries exceeded")
    
    def check_if_stuck(self):
        """Check if vehicle is stuck in one position"""
        # Skip stuck detection in SITL if disabled
        if self.sitl_mode and not self.enable_stuck_detection:
            return
            
        if not self.position_history or len(self.position_history) < 5:
            return
        
        current_time = time.time()
        
        # Adjust parameters for SITL (might move slower in simulation)
        stuck_radius = self.stuck_radius * 2 if self.sitl_mode else self.stuck_radius
        stuck_time = self.stuck_time * 1.5 if self.sitl_mode else self.stuck_time
        
        # Check positions from stuck_time seconds ago
        old_positions = [p for p in self.position_history 
                        if current_time - p['time'] > stuck_time]
        
        if not old_positions:
            return
        
        # Calculate movement from oldest position
        old_pos = old_positions[0]
        current_pos = self.position_history[-1]
        
        distance = math.sqrt(
            (current_pos['x'] - old_pos['x'])**2 +
            (current_pos['y'] - old_pos['y'])**2
        )
        
        if distance < stuck_radius:
            if not self.stuck_detected:
                self.stuck_detected = True
                self.mission_stats['stuck_events'] += 1
                
                if self.sitl_mode:
                    # In SITL, just warn but don't trigger RTL
                    self.get_logger().warn(f'⚠️ Vehicle stuck in SITL! Moved only {distance:.1f}m in {stuck_time:.0f}s')
                else:
                    # In real craft, take action
                    self.get_logger().warn(f'⚠️ Vehicle stuck! Moved only {distance:.1f}m in {stuck_time:.0f}s')
                    
                    # Don't trigger RTL if already in LOITER or RTL
                    current_mode = self.get_current_mode()
                    if current_mode not in ['AUTO.LOITER', 'AUTO.RTL', 'AUTO.LAND']:
                        self.trigger_rtl(f"Stuck detection - moved only {distance:.1f}m")
        else:
            self.stuck_detected = False
    
    def send_rtl_command(self):
        """Send RTL command"""
        current_time = time.time()
        
        if current_time - self.last_command_time < self.command_cooldown:
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
        self.last_command_time = current_time
        return True
    
    def trigger_rtl(self, reason):
        """Request RTL"""
        current_mode = self.get_current_mode()
        if current_mode in ['AUTO.RTL', 'AUTO.LAND']:
            return
        
        self.get_logger().warn(f'📡 MISSION SUPERVISOR requesting RTL: {reason}')
        self.rtl_trigger_source = f"Mission Supervisor: {reason}"
        
        self.send_rtl_command()
    
    def supervise_mission(self):
        """Main supervision logic"""
        with self.data_lock:
            # Skip if not armed or no mission active
            if not self.is_armed() or not self.mission_active:
                return
            
            # Skip during grace period
            if self.is_in_grace_period():
                return
            
            # Skip if failsafe active
            if self.is_failsafe_active():
                return
            
            current_time = time.time()
            current_mode = self.get_current_mode()
            
            # Skip checks if already in emergency mode
            if current_mode in ['AUTO.RTL', 'AUTO.LAND']:
                return
            
            # Check altitude before actions (less strict in SITL)
            min_alt = self.min_altitude / 2 if self.sitl_mode else self.min_altitude
            if self.current_altitude < min_alt:
                return
            
            # Check mission timeout (more lenient in SITL)
            if self.mission_start_time:
                timeout = self.mission_timeout * 2 if self.sitl_mode else self.mission_timeout
                mission_duration = current_time - self.mission_start_time
                if mission_duration > timeout:
                    self.get_logger().error(f'⏱️ Mission timeout ({mission_duration/60:.1f} min)')
                    self.trigger_rtl('Mission timeout')
                    return
            
            # Check waypoint timeout (more lenient in SITL)
            if self.waypoint_start_time and self.last_waypoint_change:
                wp_timeout = self.waypoint_timeout * 2 if self.sitl_mode else self.waypoint_timeout
                waypoint_duration = current_time - self.last_waypoint_change
                if waypoint_duration > wp_timeout:
                    if self.sitl_mode:
                        self.get_logger().warn(f'⏱️ Waypoint timeout in SITL ({waypoint_duration:.0f}s) - monitoring')
                    else:
                        self.get_logger().warn(f'⏱️ Waypoint timeout ({waypoint_duration:.0f}s)')
                        self.trigger_rtl(f'Waypoint {self.current_waypoint} timeout')
                    return
            
            # Check for extended loiter (might indicate problem)
            if self.loiter_start_time and not self.rtl_completed and not self.mission_paused:
                loiter_duration = current_time - self.loiter_start_time
                # More lenient in SITL
                loiter_threshold = 120 if self.sitl_mode else 60
                if loiter_duration > loiter_threshold:
                    self.get_logger().warn(f'⚠️ Extended loiter detected ({loiter_duration:.0f}s)')
    
    def publish_status(self):
        """Publish mission supervisor status"""
        with self.data_lock:
            mission_time = 0
            if self.mission_start_time:
                mission_time = time.time() - self.mission_start_time
            
            status = {
                'timestamp': time.time(),
                'state': self.mission_state.name,
                'mode': self.get_current_mode(),
                'armed': self.is_armed(),
                'mission_active': self.mission_active,
                'mission_paused': self.mission_paused,
                'current_waypoint': self.current_waypoint,
                'total_waypoints': self.total_waypoints,
                'mission_time': mission_time,
                'altitude': self.current_altitude,
                'stuck_detected': self.stuck_detected,
                'statistics': self.mission_stats,
                'rtl_trigger_source': self.rtl_trigger_source
            }
            
            msg = String()
            msg.data = json.dumps(status)
            self.mission_status_pub.publish(msg)
            
            # Log status periodically
            current_time = time.time()
            if current_time - self.last_status_log_time > 10 and self.mission_active:
                self.get_logger().info(
                    f'📋 Mission: WP {self.current_waypoint}/{self.total_waypoints}, '
                    f'Time: {mission_time/60:.1f}min, Alt: {self.current_altitude:.1f}m'
                )
                self.last_status_log_time = current_time

def main(args=None):
    rclpy.init(args=args)
    node = MissionSupervisor()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        node.get_logger().info('Shutting down Mission Supervisor')
    finally:
        node.destroy_node()
        rclpy.shutdown()

if __name__ == '__main__':
    main()
