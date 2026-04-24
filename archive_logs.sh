#!/bin/bash

# Define paths
LOCAL_DIR="/home/ccreehan/Pi_Network_Monitor/logs"
REMOTE_DIR="gdrive:Network_Monitor_Archive"

# Move logs older than 30 days to the cloud, and record the action in the error log
rclone move "$LOCAL_DIR" "$REMOTE_DIR" --min-age 30d --log-file /home/ccreehan/Pi_Network_Monitor/system_errors.log --log-level INFO
