#!/bin/bash
set -e
install -m 755 /home/ez/sapb1-ai/deploy/power/ai-power-switch /usr/local/bin/ai-power-switch
install -m 644 /home/ez/sapb1-ai/deploy/power/ai-power-switch.service /etc/systemd/system/ai-power-switch.service
# Unload the AI model 5 minutes after the last question (was 10), so the PC drops to low power sooner
cp /etc/systemd/system/ollama.service.d/override.conf /etc/systemd/system/ollama.service.d/override.conf.bak
sed -i 's/OLLAMA_KEEP_ALIVE=10m/OLLAMA_KEEP_ALIVE=5m/' /etc/systemd/system/ollama.service.d/override.conf
systemctl daemon-reload
systemctl restart ollama
systemctl enable --now ai-power-switch
sleep 4
systemctl --no-pager status ai-power-switch | head -8
echo "CPU energy preference now: $(cat /sys/devices/system/cpu/cpu0/cpufreq/energy_performance_preference)"
