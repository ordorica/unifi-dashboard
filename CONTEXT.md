# UniFi Live Dashboard

A live view of one UniFi network: what hardware exists, how the internet
connections are performing, and which clients are on which VLAN.

## Language

### Network edge

**WAN Path**:
One internet connection as the controller reports it, identified by its own
inventory key (`WAN`, `WAN3`). A path has an ISP, a link type, a status and its
own throughput and latency. It is not a device.
_Avoid_: WAN, uplink, circuit, gateway (when a path is meant)

**Gateway Device**:
A physical box that routes traffic and reports its own CPU, memory, load,
temperature and uptime. A Gateway Device may own zero, one, or many WAN Paths.
_Avoid_: gateway, router, WAN (when a device is meant)

**Cellular Modem**:
A Gateway Device whose hardware is a cellular radio. Being a Cellular Modem says
nothing about whether any WAN Path is cellular — on this network the cellular
WAN Path is a GRE tunnel owned by the main Gateway Device, while the Cellular
Modem owns no WAN Path at all.
_Avoid_: cellular gateway, LTE gateway, failover device

**Link Type**:
How a WAN Path physically connects, as reported by the controller
(`ethernet`, `wireless_5g`, …). The sole basis for deciding whether a WAN Path
is cellular.
_Avoid_: connection type, medium, WAN kind

### Measurement

**Speedtest Attribution**:
Determining which WAN Path a recorded speedtest ran over. Attribution is only
possible when the controller's live status can be matched to an archived
record; unattributed speedtests stay unattributed rather than being inferred.
_Avoid_: speedtest source, speedtest classification

## Explicitly rejected

**"Primary" / "Cellular" as WAN Path identities**:
These conflate a Gateway Device with a WAN Path and assume exactly two internet
connections, one of them cellular. A WAN Path is identified by its controller
key; whether it is cellular comes from its Link Type.
