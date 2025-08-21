## Modify PX4 code in order to be able to subscribe to these topics
```bash
#In terminal
cd PX4-Autopilot/src/modules/uxrce_dds_client
gedit dds_topics.yaml
---
#Add under publications

  - topic: /fmu/out/mission_result
    type: px4_msgs::msg::MissionResult
  
  - topic: /fmu/out/geofence_result
    type: px4_msgs::msg::GeofenceResult
    
  - topic: /fmu/out/vehicle_imu_status
    type: px4_msgs::msg::VehicleImuStatus
  
  - topic: /fmu/out/sensor_mag
    type: px4_msgs::msg::SensorMag
    
  - topic: /fmu/out/sensor_baro
    type: px4_msgs::msg::SensorBaro
  
  - topic: /fmu/out/vehicle_air_data
    type: px4_msgs::msg::VehicleAirData
  
  - topic: /fmu/out/rc_channels
    type: px4_msgs::msg::RcChannels
    

#Run the following command in terminal
python3 generate_dds_topics.py -m ../../msg -y dds_topics.yaml -u . -t dds_topics.h.em

```

## Modify battery_simulator in order for the SITL to be as realistic as possible
```bash
#In terminal
cd /PX4-Autopilot/src/modules/simulation/battery_simulator
gedit BatterySimulator.cpp

#Comment out this line:
float ibatt = -1.0f; // no current sensor in simulation	

#Instead add this: 
float ibatt = _armed ? 15.0f : 0.0f;  // Fixed 2A when armed, 0A when disarmed

```
## Prerequisites
- ROS 2 Jazzy
- PX4 main version
- MicroXRCE-DDS agent
- px4.msgs for main px4 version

## Architecture

The system consists of three main ROS 2 nodes:

### 1. PowerMonitor Node
- Monitors battery status and power consumption
- Calculates remaining flight time and return-to-launch requirements
- Triggers automatic RTL when battery reaches critical levels
- Uses Haversine formula for distance calculations

### 2. FaultDetector Node
- Monitors GPS integrity and system health
- Detects GPS signal loss and sensor failures
- Triggers emergency landing when pcritical systems fail
- Configurable thresholds for GPS fix quality

### 3. MissionSupervisor Node
- Central coordination and decision-making
- Monitors communication links and mission duration
- Implements failsafe backup logic
- Maintains mission state machine
