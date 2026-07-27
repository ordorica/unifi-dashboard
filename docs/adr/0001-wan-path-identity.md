# WAN Paths get synthetic identities, matched conservatively

A WAN Path's identity could have been its controller key (`WAN`, `WAN3`), but
that key is a slot, not a service: re-cabling a link splits its history, and
swapping ISPs on a slot silently blends two different services into one chart.
We instead keep a `wan_paths` table of synthetic ids and match each sighting to
an existing path, so history follows the internet service rather than the socket
it happens to be plugged into.

## Considered Options

Identity by controller key was the simpler choice and was recommended at design
time. It handles duplicate ISPs cleanly and needs no matching logic at all, but
it fails on the scenario we judged most likely on a home network — changing
provider on an existing WAN.

## Consequences

Matching is a heuristic, and this project elsewhere refuses heuristics: the
speedtest latency threshold was removed for exactly that reason. The
distinction we are drawing is that the latency threshold guessed silently,
invisibly and unfixably, whereas path matching is **conservative, visible and
correctable**:

- Candidates are existing paths on the same gateway with the same ASN.
- Exactly one candidate means the same path. Several means prefer the same
  controller key, then the same interface name.
- Any remaining ambiguity, or a missing ASN with no key/interface match,
  creates a **new** path.
- `wan_paths.label_override` lets a human correct a bad split.

The rule deliberately fails toward splitting. A wrong split shows two series
where one was expected — visible and fixable. A wrong merge blends two services
irreversibly, because the rows no longer record which was which.

Two known rough edges follow from this: a WAN that is down when first seen has
no ASN and may register as its own path until geo data arrives, and two links
from the same provider that swap slots will split rather than follow the cable.
