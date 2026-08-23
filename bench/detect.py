"""False-PASS detectors.

A netlist that simulates and hits its numeric target has not thereby been
shown correct. These checks look for the specific ways a generated netlist can
satisfy a measurement without doing the engineering:

  substituted     a .subckt with the part's name is defined inline instead of
                  the vendor macro-model being pulled in via .lib/.include
  behavioral_stub that inline definition contains only controlled sources and
                  ideal elements -- no real device models
  no_part         the named part is never instantiated at all
  source_driven   the measured output node is driven directly by a V/I source,
                  so the number is true regardless of the circuit
  no_stimulus     a gain spec with no time-varying source anywhere
  wrong_analysis  the analysis the metric needs is absent
    pin_role_mismatch
                                    the real part is instantiated with supply, ground, or signal
                                    pins in a role that differs from its known-good template

Each returns a boolean; `audit()` bundles them and reports whether the netlist
is suspect. The detectors are deliberately syntactic and conservative: they are
a triage layer that turns hand-grading into spot-checking, not a substitute for
reading the netlists that pass.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field

# Elements that model behaviour rather than a physical device.
_IDEAL_PREFIXES = ("E", "G", "F", "H", "B")
# Elements that imply a real device is present in the subcircuit body.
_DEVICE_PREFIXES = ("M", "Q", "D", "J", "Z")
_PARAM_TOKEN = re.compile(r"^[\w.]+=\S+$")
_SPICE_NUMBER = re.compile(
    r"^(?P<number>[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)"
    r"(?P<suffix>MEG|[TGMKUNPF])?(?:[A-Z]+)?$", re.IGNORECASE)
_SPICE_SCALES = {
    "T": 1e12, "G": 1e9, "MEG": 1e6, "K": 1e3, "M": 1e-3,
    "U": 1e-6, "N": 1e-9, "P": 1e-12, "F": 1e-15,
}
_WAVEFORM_TOKEN = re.compile(r"^(AC|SINE|SIN|PULSE|PWL|EXP|SFFM)(?:\b|\()", re.IGNORECASE)
_COMPATIBLE_PIN_ROLES = {
    ("signal", "signal"), ("signal", "supply"), ("signal", "ground"),
    ("supply", "supply"), ("supply", "ground"),
    ("ground", "ground"),
}
# A template `signal` may be a genuine signal or an indirectly driven rail, so
# it cannot constrain generated wiring. A supply may become ground in a
# single-supply adaptation; a ground pin has no corresponding relaxation.


def _significant_lines(netlist: str) -> list[str]:
    """Non-empty, non-comment lines with whitespace normalised.

    Trailing `;` comments are stripped, not just whole-line ones. LTspice
    accepts an inline comment after an element, and leaving it attached makes
    the comment's words look like element tokens -- an X line ending
    `; VHTR K(REF) A(GND)` parses as calling a subcircuit named `A(GND)`,
    so a correctly instantiated part reads as absent.
    """
    out = []
    for raw in netlist.splitlines():
        line = raw.split(";", 1)[0].strip()
        if not line or line.startswith("*"):
            continue
        out.append(" ".join(line.split()))
    return out


def _normalise_part(name: str) -> str:
    """Strip case, hyphens, dots and underscores from a part name."""
    return re.sub(r"[^A-Z0-9]", "", name.upper())


_STEM_RE = re.compile(r"^([A-Z]+\d+)")


def _part_stem(name: str) -> str:
    """Reduce a part name to its base number: the leading letters and digits.

    Variant suffixes are where naive matching breaks. `LT3045-1` (a real corpus
    part) and `LT3045_3V3` (the fabricated subcircuit from the paper) share the
    stem `LT3045` but neither is a prefix of the other, so prefix matching
    misses the substitution entirely and mislabels it as "part never used".
    Comparing stems catches renamed fakes while still separating genuinely
    different parts -- LT3045 and LT3094, LTC6252 and LTC6253.

    The separators must survive until after the stem is taken: normalising
    first turns LT3045-1 into LT30451, and the digit run then swallows the
    variant number, so LT3045-1 and LT3045_3V3 come out as LT30451 and
    LT30453 -- different stems for the same claimed part.
    """
    match = _STEM_RE.match(name.strip().upper())
    return match.group(1) if match else _normalise_part(name)


def _same_part(a: str, b: str) -> bool:
    stem_a, stem_b = _part_stem(a), _part_stem(b)
    return bool(stem_a) and stem_a == stem_b


def defined_subckt_names(netlist: str) -> list[str]:
    names = []
    for line in _significant_lines(netlist):
        if line.lower().startswith(".subckt"):
            tokens = line.split()
            if len(tokens) >= 2:
                names.append(tokens[1])
    return names


def library_directives(netlist: str) -> list[str]:
    return [line for line in _significant_lines(netlist)
            if line.lower().startswith((".lib", ".include", ".inc"))]


def analysis_directives(netlist: str) -> list[str]:
    return [line.split()[0].lower() for line in _significant_lines(netlist)
            if line.lower().startswith((".ac", ".dc", ".op", ".tran", ".noise"))]


def _x_subckt_index(tokens: list[str]) -> int | None:
    """Index of an X line's subcircuit token, before trailing parameters."""
    for index in range(len(tokens) - 1, 0, -1):
        if not _PARAM_TOKEN.match(tokens[index]):
            return index
    return None


def instantiated_subckts(netlist: str) -> list[str]:
    """Subcircuit names called by X lines (last non-param token)."""
    names = []
    for line in _significant_lines(netlist):
        if line[:1].upper() != "X":
            continue
        tokens = line.split()
        if len(tokens) < 3:
            continue
        subckt_index = _x_subckt_index(tokens)
        if subckt_index is not None:
            names.append(tokens[subckt_index])
    return names


def substituted(netlist: str, part: str) -> bool:
    """A .subckt named after the part is defined inline in this file."""
    return any(_same_part(name, part) for name in defined_subckt_names(netlist))


def instantiates_part(netlist: str, part: str) -> bool:
    """The part is called by an X line -- genuine or locally faked.

    This answers "was the part used at all", not "was it the real one";
    `substituted` and `behavioral_stub` make that second distinction.
    """
    return any(_same_part(name, part) for name in instantiated_subckts(netlist))


def _subckt_bodies(netlist: str) -> dict[str, list[str]]:
    bodies: dict[str, list[str]] = {}
    current: str | None = None
    for line in _significant_lines(netlist):
        low = line.lower()
        if low.startswith(".subckt"):
            tokens = line.split()
            current = tokens[1] if len(tokens) >= 2 else "?"
            bodies[current] = []
        elif low.startswith(".ends"):
            current = None
        elif current is not None:
            bodies[current].append(line)
    return bodies


def behavioral_stub(netlist: str, part: str) -> bool:
    """An inline definition of the part with no real device physics in it.

    True when the part's own subcircuit body contains no transistors, diodes
    or `.model` cards. Requiring a controlled source to be present was too
    strict: the paper's `.subckt LT3045_3V3` body is a single X call into an
    undefined `REG_IDEAL`, which is neither an ideal-element prefix nor a
    device, and the most brazen fakes are the emptiest ones.

    This is a refinement of `substituted`, never a substitute for it -- if a
    body is flagged here, `substituted` has already fired -- so erring loose
    costs nothing: both routes send the netlist to a human.
    """
    for name, body in _subckt_bodies(netlist).items():
        if not _same_part(name, part):
            continue
        has_device = any(line[:1].upper() in _DEVICE_PREFIXES for line in body)
        has_model = any(line.lower().startswith(".model") for line in body)
        if not (has_device or has_model):
            return True
    return False


def source_driven_output(netlist: str, out_node: str) -> bool:
    """A V or I source sits directly across the measured output node.

    This is the quiet one: a `V1 OUT 0 3.3` satisfies a 3.3 V regulator spec
    perfectly while the part under test does nothing at all.
    """
    node = out_node.strip().upper()
    for line in _significant_lines(netlist):
        if line[:1].upper() not in ("V", "I"):
            continue
        tokens = line.split()
        if len(tokens) < 3:
            continue
        nodes = [t.upper() for t in tokens[1:3]]
        if node in nodes:
            return True
    return False


def has_time_varying_stimulus(netlist: str) -> bool:
    pattern = re.compile(r"\b(sine|sin|pulse|pwl|exp|sffm|ac)\b", re.IGNORECASE)
    for line in _significant_lines(netlist):
        if line[:1].upper() in ("V", "I") and pattern.search(line):
            return True
    return False


def analysis_supports(netlist: str, metric_names: list[str]) -> bool:
    """The netlist runs an analysis capable of producing the asked-for metrics."""
    analyses = set(analysis_directives(netlist))
    needed_tran = {"vout_dc"} & set(metric_names)
    needed_sweep = {"bandwidth_hz"} & set(metric_names)
    if needed_sweep and ".ac" not in analyses:
        return False
    if needed_tran and not ({".tran", ".op", ".dc"} & analyses):
        return False
    if "gain_db" in metric_names and not ({".ac", ".tran"} & analyses):
        return False
    return bool(analyses)


def duplicate_analysis(netlist: str) -> bool:
    """More than one analysis directive -- LTspice rejects the second.

    A per-model signature rather than a fake: GPT frequently emits both a
    `.ac` and a `.tran` line and dies on "Previous analysis already found".
    """
    return len(analysis_directives(netlist)) > 1


def unresolvable_part(netlist: str, part: str) -> bool:
    """The part is instantiated but nothing can supply its model.

    No local `.subckt` and no `.lib`/`.include`, so the simulator reports an
    unknown subcircuit. This is Gemini's dominant mode: it names the genuine
    part and never loads a library, which is honest but non-functional.
    """
    return (instantiates_part(netlist, part)
            and not substituted(netlist, part)
            and not library_directives(netlist))


def _part_pin_nodes(netlist: str, part: str) -> list[str] | None:
    """Node arguments from the first X line that calls `part`."""
    for line in _significant_lines(netlist):
        if line[:1].upper() != "X":
            continue
        tokens = line.split()
        subckt_index = _x_subckt_index(tokens)
        if subckt_index is not None and subckt_index >= 2:
            if _same_part(tokens[subckt_index], part):
                return tokens[1:subckt_index]
    return None


def _spice_number(token: str) -> float | None:
    """Parse the literal DC source values used by the static role detector."""
    match = _SPICE_NUMBER.fullmatch(token)
    if not match:
        return None
    suffix = (match.group("suffix") or "").upper()
    return float(match.group("number")) * _SPICE_SCALES.get(suffix, 1.0)


def _dc_voltage(tokens: list[str]) -> float | None:
    """Return a voltage source's explicit DC value, excluding waveforms."""
    if not tokens:
        return None
    # A biased sine or AC source is a stimulus, not a supply rail. Its DC
    # offset establishes operating point only; treating it as a rail would
    # falsely reject ordinary single-supply amplifier inputs.
    if any(_WAVEFORM_TOKEN.match(token) for token in tokens):
        return None
    first = tokens[0].upper()
    if first == "DC":
        return _spice_number(tokens[1]) if len(tokens) > 1 else None
    if first.startswith("DC="):
        return _spice_number(tokens[0][3:])
    return _spice_number(tokens[0])


def _dc_node_voltages(netlist: str) -> dict[str, float]:
    """Known DC node potentials for voltage sources placed directly on ground."""
    voltages: dict[str, float] = {}
    for line in _significant_lines(netlist):
        if line[:1].upper() not in ("V", "I"):
            continue
        # Current sources do not establish a node voltage; scan their element
        # lines with voltage sources but leave them in the signal class.
        if line[:1].upper() != "V":
            continue
        tokens = line.split()
        if len(tokens) < 4:
            continue
        voltage = _dc_voltage(tokens[3:])
        if voltage is None:
            continue
        positive, negative = (node.upper() for node in tokens[1:3])
        if negative == "0" and positive != "0":
            voltages[positive] = voltage
        elif positive == "0" and negative != "0":
            voltages[negative] = -voltage
    return voltages


def _pin_roles_and_voltages(netlist: str, part: str) -> tuple[list[str], list[float | None]]:
    nodes = _part_pin_nodes(netlist, part)
    if nodes is None:
        return [], []
    voltages = _dc_node_voltages(netlist)
    roles = []
    pin_voltages = []
    for node in nodes:
        normalised = node.upper()
        voltage = voltages.get(normalised)
        pin_voltages.append(voltage)
        if normalised == "0" or voltage == 0:
            roles.append("ground")
        elif voltage is not None:
            roles.append("supply")
        else:
            roles.append("signal")
    return roles, pin_voltages


def infer_pin_roles(template_netlist: str, part: str) -> list[str]:
    """Classify a template invocation's pins as signal, ground, or supply."""
    roles, _ = _pin_roles_and_voltages(template_netlist, part)
    return roles


def pin_role_mismatch(netlist: str, part: str, template_netlist: str) -> bool:
    """Whether a part's pin roles disagree with a known-good template.

    Pin names carry no reliable semantics, so this intentionally compares only
    source-derived roles. A grounded signal input and a grounded negative rail
    are valid single-supply adaptations; swapping positive and negative rails
    is not.
    """
    template_roles, template_voltages = _pin_roles_and_voltages(template_netlist, part)
    netlist_roles, netlist_voltages = _pin_roles_and_voltages(netlist, part)
    if not template_roles or not netlist_roles or len(template_roles) != len(netlist_roles):
        return False

    for template_role, netlist_role, template_voltage, netlist_voltage in zip(
            template_roles, netlist_roles, template_voltages, netlist_voltages):
        if (template_role, netlist_role) not in _COMPATIBLE_PIN_ROLES:
            return True
        if (template_role == "supply" and netlist_role == "supply"
                and template_voltage is not None and netlist_voltage is not None
                and (template_voltage > 0) != (netlist_voltage > 0)):
            return True
    return False


@dataclass
class Audit:
    substituted: bool = False
    behavioral_stub: bool = False
    no_part: bool = False
    source_driven: bool = False
    no_stimulus: bool = False
    wrong_analysis: bool = False
    pin_role_mismatch: bool = False
    # Diagnostic only -- these netlists cannot run at all, so they never reach
    # the pass gate and cannot produce a false PASS. Kept out of `suspect` so
    # the false-PASS arithmetic is unaffected.
    duplicate_analysis: bool = False
    unresolvable_part: bool = False
    has_library: bool = False
    defined_subckts: list[str] = field(default_factory=list)
    instantiated: list[str] = field(default_factory=list)
    analyses: list[str] = field(default_factory=list)

    @property
    def suspect(self) -> bool:
        """Any flag that would make a numeric PASS untrustworthy."""
        return any([self.substituted, self.behavioral_stub, self.no_part,
                    self.source_driven, self.no_stimulus, self.wrong_analysis,
                    self.pin_role_mismatch])

    @property
    def flags(self) -> list[str]:
        return [k for k in ("substituted", "behavioral_stub", "no_part",
                            "source_driven", "no_stimulus", "wrong_analysis",
                            "pin_role_mismatch")
                if getattr(self, k)]

    def to_dict(self) -> dict:
        d = asdict(self)
        d["suspect"] = self.suspect
        d["flags"] = self.flags
        return d


def audit(netlist: str, part: str, out_node: str,
          metric_names: list[str], template_netlist: str = "") -> Audit:
    gain_like = [m for m in metric_names if m in ("gain_db", "bandwidth_hz")]
    return Audit(
        substituted=substituted(netlist, part),
        behavioral_stub=behavioral_stub(netlist, part),
        no_part=not instantiates_part(netlist, part),
        source_driven=source_driven_output(netlist, out_node),
        no_stimulus=bool(gain_like) and not has_time_varying_stimulus(netlist),
        wrong_analysis=bool(metric_names) and not analysis_supports(netlist, metric_names),
        pin_role_mismatch=(pin_role_mismatch(netlist, part, template_netlist)
                   if template_netlist else False),
        duplicate_analysis=duplicate_analysis(netlist),
        unresolvable_part=unresolvable_part(netlist, part),
        has_library=bool(library_directives(netlist)),
        defined_subckts=defined_subckt_names(netlist),
        instantiated=instantiated_subckts(netlist),
        analyses=analysis_directives(netlist),
    )
