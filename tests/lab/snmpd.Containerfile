# The device lab's SNMP agent (scripts/device-lab.sh): net-snmp on Alpine, answering SNMPv2c (a read-only community)
# and SNMPv3 authPriv (SHA/AES) on UDP 1161. Lab-only secrets: nothing here guards anything.
FROM docker.io/library/alpine:3.20.3
RUN apk add --no-cache net-snmp=5.9.4-r0
RUN printf '%s\n' \
    'agentaddress udp:1161' \
    'rocommunity labcommunity' \
    'createUser labv3 SHA "labauthpass" AES "labprivpass"' \
    'rouser labv3 priv' \
    'sysDescr Pengumon lab SNMP agent (net-snmp)' > /etc/snmp/snmpd.conf
# -C: only this file (with -c alone the default path is read too, and the second bind of the port fails)
CMD ["snmpd", "-f", "-Lo", "-C", "-c", "/etc/snmp/snmpd.conf"]
