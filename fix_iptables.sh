#!/bin/bash
BRIDGE="br-c2989561f8a3"
iptables -I DOCKER-CT 1 -o $BRIDGE -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT
iptables -I DOCKER-CT 2 -i $BRIDGE ! -o $BRIDGE -j ACCEPT
iptables -I DOCKER-CT 3 -i $BRIDGE -o $BRIDGE -j ACCEPT
iptables -I FORWARD 1 -i $BRIDGE -o $BRIDGE -j ACCEPT
echo "iptables rules applied for $BRIDGE"
