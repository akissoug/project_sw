## Modify PX4 code in order to be able to subscribe to these topics
```bash
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
    

#Run the following command

python3 generate_dds_topics.py -m ../../msg -y dds_topics.yaml -u . -t dds_topics.h.em

```

##Modify battery_simulator in order for the SITL to be as realistic as possible
```bash
cd /PX4-Autopilot/src/modules/simulation/battery_simulator
gedit BatterySimulator.cpp

#Comment out this line:
float ibatt = -1.0f; // no current sensor in simulation	

#Instead add this: 
float ibatt = _armed ? 15.0f : 0.0f;  // Fixed 2A when armed, 0A when disarmed

```

