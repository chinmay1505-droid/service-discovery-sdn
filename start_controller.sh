#!/bin/bash
mkdir -p logs
~/sdn-venv/bin/osken-manager sdn/sdn_controller.py 2>&1 | tee logs/controller.log
