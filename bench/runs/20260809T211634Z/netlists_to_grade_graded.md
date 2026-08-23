# Netlists needing your judgement

20 netlists. All of them already simulate and already hit their numeric target.
That is settled -- do not re-check it.

**The one question:** did the real part produce this output, or would the circuit
have produced it anyway?

Answer each with one word: **REAL** / **FAKE** / **UNSURE**

- REAL  = the vendor part is doing the work and the circuit is what was asked for
- FAKE  = the number arrives some other way (source drives the output directly,
          divider sets it, part is bypassed, output distorted beyond usefulness)
- UNSURE = say so, don't guess

---

## 1. B1 — AD8092 — gemini-3.1-pro-preview k2 [bare arm]

- **Asked for:** Non-inverting amplifier with a closed-loop gain of 5 V/V on +/-5 V supplies, driven by a 100 mV amplitude sine at 1 MHz, with a load resistor on the output.
- **Measured:** gain_db=13.95 (target 13.98)
- **Why you're looking:** distorted output (THD=0.159)
- **Look at:** is `OUT` reached *through* the AD8092, or is it set by a source/divider that would hold that value with the part removed?

```spice
* AD8092 Non-Inverting Amplifier
V1 IN 0 SINE(0 100m 1Meg) AC 1
V2 VCC 0 5
V3 VEE 0 -5

* AD8092 Op-Amp Subcircuit (IN+ IN- V+ V- OUT)
X1 IN IN_MINUS VCC VEE OUT AD8092

* Feedback Network for Gain of 5 V/V (1 + R1/R2 = 5)
R1 OUT IN_MINUS 400
R2 IN_MINUS 0 100

* Output Load Resistor
R3 OUT 0 1k

* Transient Analysis for 5 cycles of 1 MHz
.tran 0 5u 0 1n

* Measure Peak-to-Peak voltages over the last 2 cycles to ensure settling
.meas tran Vout_pp pp V(OUT) from=3u to=5u
.meas tran Vin_pp pp V(IN) from=3u to=5u

* Calculate Closed-Loop Gain in dB
.meas tran gain_db param 20*log10(Vout_pp/Vin_pp)

* Include Standard Analog Devices Libraries
.lib ADI.lib
.lib ADI1.lib

.end
```

**Verdict:** FAKE

---

## 2. B4 — AD8551 — gemini-3.1-pro-preview k2 [bare arm]

- **Asked for:** Non-inverting amplifier with a gain of 100 V/V on a single 5 V supply, driven by a 10 mV amplitude sine at 1 kHz.
- **Measured:** gain_db=39.91 (target 40.0)
- **Why you're looking:** waveform too short to analyse
- **Look at:** is `OUT` reached *through* the AD8551, or is it set by a source/divider that would hold that value with the part removed?

```spice
* AD8551 Non-inverting Amplifier
V1 IN 0 SINE(25m 10m 1k)
V2 VCC 0 5V
R1 VM 0 1k
R2 OUT VM 99k
X1 IN VM VCC 0 OUT AD8551

.lib ADI.lib

.tran 0 5m 0 1u
.meas tran in_pp PP V(IN) FROM 3m TO 5m
.meas tran out_pp PP V(OUT) FROM 3m TO 5m
.meas tran gain_db param 20*log10(out_pp/in_pp)

.end
```

**Verdict:** REAL

---

## 3. A4 — ADR1399H — gpt-5.6-sol k1 [library arm]

- **Asked for:** Precision voltage reference producing its nominal 7.05 V output from a 15 V supply, with an output bypass capacitor and a light load.
- **Measured:** vout_dc=7.05 (target 7.05)
- **Why you're looking:** no waveform check possible (DC metric)
- **Look at:** is `OUT` reached *through* the ADR1399H, or is it set by a source/divider that would hold that value with the part removed?

```spice
* ADR1399H 7.05 V precision shunt reference
.lib ADR1399H.sub

V1 IN 0 15
RBIAS IN OUT 2.61k
XREF OUT 0 IN 0 ADR1399H
COUT OUT 0 10u
RLOAD OUT 0 100k

.tran 0 20 0 10m startup
.meas TRAN vout_dc AVG V(OUT) FROM 19 TO 20

.end
```

**Verdict:** REAL

---

## 4. A4 — ADR1399H — gpt-5.6-sol k3 [library arm]

- **Asked for:** Precision voltage reference producing its nominal 7.05 V output from a 15 V supply, with an output bypass capacitor and a light load.
- **Measured:** vout_dc=7.05 (target 7.05)
- **Why you're looking:** no waveform check possible (DC metric)
- **Look at:** is `OUT` reached *through* the ADR1399H, or is it set by a source/divider that would hold that value with the part removed?

```spice
* ADR1399H 7.05 V precision shunt reference
.lib ADR1399H.sub

V1 IN 0 15
RBIAS IN OUT 2.49k
XU1 OUT 0 0 IN ADR1399H

COUT OUT 0 1u
RLOAD OUT 0 100k

.tran 0 20 0 10m
.meas tran vout_dc AVG V(OUT) FROM=19 TO=20

.end
```

**Verdict:** REAL

---

## 5. A4 — ADR1399H — gemini-3.1-pro-preview k1 [library arm]

- **Asked for:** Precision voltage reference producing its nominal 7.05 V output from a 15 V supply, with an output bypass capacitor and a light load.
- **Measured:** vout_dc=7.04 (target 7.05)
- **Why you're looking:** no waveform check possible (DC metric)
- **Look at:** is `OUT` reached *through* the ADR1399H, or is it set by a source/divider that would hold that value with the part removed?

```spice
* ADR1399H Precision Voltage Reference
V1 IN 0 15
R1 IN OUT 2.67k
C1 OUT 0 1u
Rload OUT 0 100k
X1 OUT 0 IN 0 ADR1399H

.lib ADR1399H.sub
.tran 10m
.end
```

**Verdict:** REAL

---

## 6. A4 — ADR1399H — gemini-3.1-pro-preview k2 [library arm]

- **Asked for:** Precision voltage reference producing its nominal 7.05 V output from a 15 V supply, with an output bypass capacitor and a light load.
- **Measured:** vout_dc=7.04 (target 7.05)
- **Why you're looking:** no waveform check possible (DC metric)
- **Look at:** is `OUT` reached *through* the ADR1399H, or is it set by a source/divider that would hold that value with the part removed?

```spice
* ADR1399H Precision Voltage Reference Circuit
* 15V Supply to 7.05V Shunt Reference

* Input Supply
V1 IN 0 15

* Bias Resistor for the Zener (Targeting ~3mA Zener current)
* (15V - 7.05V) / 2.67kOhm ≈ 2.98mA
R1 IN OUT 2.67k

* ADR1399H Subcircuit Instantiation
* Standard pinout for LM399/ADR1399 models: Zener+ Zener- Heater+ Heater-
X1 OUT 0 IN 0 ADR1399H

* Output Bypass Capacitor
C1 OUT 0 1u

* Light Load (100kOhm draws ~70.5µA)
Rload OUT 0 100k

* Library Include
.lib ADR1399H.sub

* Analysis Directive
* Run a transient analysis long enough to settle any thermal model dynamics
.tran 100m

* Measurement Directive
* Measure the settled DC output voltage
.measure tran vout_dc avg V(OUT) from 50m to 100m

.end
```

**Verdict:** REAL

---

## 7. A4 — ADR1399H — gemini-3.1-pro-preview k3 [library arm]

- **Asked for:** Precision voltage reference producing its nominal 7.05 V output from a 15 V supply, with an output bypass capacitor and a light load.
- **Measured:** vout_dc=7.05 (target 7.05)
- **Why you're looking:** no waveform check possible (DC metric)
- **Look at:** is `OUT` reached *through* the ADR1399H, or is it set by a source/divider that would hold that value with the part removed?

```spice
* ADR1399H Precision Voltage Reference
V1 IN 0 15
R1 IN OUT 2.7k
X1 OUT 0 IN 0 ADR1399H
C1 OUT 0 1u
R2 OUT 0 100k

* The ADR1399 incorporates a heater that takes time to reach thermal equilibrium.
* We run the simulation for 5 seconds and only save the last 100ms to ensure 
* the output voltage is measured after the heater has fully stabilized.
.tran 0 5 4.9

.lib ADR1399H.sub
.end
```

**Verdict:** REAL

---

## 8. B1 — AD8092 — gemini-3.1-pro-preview k2 [library arm]

- **Asked for:** Non-inverting amplifier with a closed-loop gain of 5 V/V on +/-5 V supplies, driven by a 100 mV amplitude sine at 1 MHz, with a load resistor on the output.
- **Measured:** gain_db=13.95 (target 13.98)
- **Why you're looking:** distorted output (THD=0.133)
- **Look at:** is `OUT` reached *through* the AD8092, or is it set by a source/divider that would hold that value with the part removed?

```spice
* AD8092 Non-Inverting Amplifier
.lib ADI.lib

* Power Supplies
VCC VCC 0 5
VEE VEE 0 -5

* Input Signal: 100 mV amplitude sine at 1 MHz
VIN IN 0 SINE(0 100m 1Meg)

* Amplifier Circuit (Gain = 1 + 1k/250 = 5 V/V)
X1 IN IN_MINUS VCC VEE OUT AD8092
R1 IN_MINUS 0 250
R2 OUT IN_MINUS 1k
RL OUT 0 1k

* Transient Analysis
.tran 0 10u 0 1n

* Measurements for Gain
.meas tran in_max MAX V(IN) FROM=5u TO=10u
.meas tran in_min MIN V(IN) FROM=5u TO=10u
.meas tran in_pp PARAM in_max-in_min

.meas tran out_max MAX V(OUT) FROM=5u TO=10u
.meas tran out_min MIN V(OUT) FROM=5u TO=10u
.meas tran out_pp PARAM out_max-out_min

.meas tran gain_db PARAM 20*log10(out_pp/in_pp)

.end
```

**Verdict:** FAKE

---

## 9. B1 — AD8092 — gemini-3.1-pro-preview k3 [library arm]

- **Asked for:** Non-inverting amplifier with a closed-loop gain of 5 V/V on +/-5 V supplies, driven by a 100 mV amplitude sine at 1 MHz, with a load resistor on the output.
- **Measured:** gain_db=13.95 (target 13.98)
- **Why you're looking:** distorted output (THD=0.133)
- **Look at:** is `OUT` reached *through* the AD8092, or is it set by a source/divider that would hold that value with the part removed?

```spice
* AD8092 Non-Inverting Amplifier G=5
.lib ADI.lib

* Power Supplies
V2 VCC 0 5
V3 VEE 0 -5

* Input Signal: 100 mV amplitude, 1 MHz sine wave
V1 IN 0 SINE(0 100m 1Meg) AC 1

* AD8092 Op-Amp Subcircuit
* Pins: IN+ IN- V+ V- OUT
X1 IN IN_N VCC VEE OUT AD8092

* Feedback Network for Gain of 5 (Rf/Rg = 4)
Rf OUT IN_N 1k
Rg IN_N 0 250

* Load Resistor
Rl OUT 0 1k

* Transient Analysis
.tran 0 10u 0 1n

* Measurements to calculate closed-loop gain in dB
.meas tran in_pp PP V(IN) FROM 5u TO 10u
.meas tran out_pp PP V(OUT) FROM 5u TO 10u
.meas tran gain_db param 20*log10(out_pp/in_pp)

.end
```

**Verdict:** FAKE

---

## 10. B2 — ADA4177 — gemini-3.1-pro-preview k1 [library arm]

- **Asked for:** Inverting amplifier with a gain of -10 V/V on +/-15 V supplies, using one input resistor and one feedback resistor, driven by a 50 mV amplitude sine at 1 kHz.
- **Measured:** gain_db=20 (target 20.0)
- **Why you're looking:** distorted output (THD=0.198)
- **Look at:** is `OUT` reached *through* the ADA4177, or is it set by a source/divider that would hold that value with the part removed?

```spice
* ADA4177 Inverting Amplifier
.lib ADA4177.lib

V1 IN 0 SINE(0 50m 1k)
Vcc VCC 0 15
Vee VEE 0 -15

R1 IN IN_MINUS 1k
R2 IN_MINUS OUT 10k

X1 0 IN_MINUS VCC VEE OUT ADA4177

.tran 0 5m
.meas tran in_pk MAX V(IN) from=2m to=5m
.meas tran out_pk MAX V(OUT) from=2m to=5m
.meas tran gain_db param 20*log10(out_pk/in_pk)

.end
```

**Verdict:** FAKE

---

## 11. B3 — LTC6078 — gpt-5.6-sol k3 [library arm]

- **Asked for:** Unity-gain voltage follower buffering a 1 kHz, 100 mV amplitude sine input on a single 5 V supply, driving a 10 k load resistor.
- **Measured:** gain_db=-0.0002512 (target 0.0)
- **Why you're looking:** waveform too short to analyse
- **Look at:** is `OUT` reached *through* the LTC6078, or is it set by a source/divider that would hold that value with the part removed?

```spice
LTC6078 Unity-Gain Voltage Follower
.lib LTC5.lib

VCC VCC 0 5
VIN IN 0 SINE(2.5 100m 1k) AC 1

XU1 IN OUT VCC 0 OUT LTC6078

RLOAD OUT 0 10k
CDEC VCC 0 100n

.tran 0 10m 5m 1u
.meas tran VIN_PP PP V(IN) FROM 5m TO 10m
.meas tran VOUT_PP PP V(OUT) FROM 5m TO 10m
.meas tran gain_db PARAM 20*log10(VOUT_PP/VIN_PP)

.end
```

**Verdict:** REAL

---

## 12. B4 — AD8551 — gemini-3.1-pro-preview k2 [library arm]

- **Asked for:** Non-inverting amplifier with a gain of 100 V/V on a single 5 V supply, driven by a 10 mV amplitude sine at 1 kHz.
- **Measured:** gain_db=39.92 (target 40.0)
- **Why you're looking:** distorted output (THD=0.212)
- **Look at:** is `OUT` reached *through* the AD8551, or is it set by a source/divider that would hold that value with the part removed?

```spice
* AD8551 Non-inverting Amplifier
.lib ADI.lib

* Single 5V Power Supply
VCC VCC 0 5

* 10 mV amplitude sine at 1 kHz, 25mV DC bias to center output at 2.5V
VIN IN 0 SINE(25m 10m 1k) AC 1

* AD8551 Op-Amp (IN+, IN-, V+, V-, OUT)
X1 IN IN_MINUS VCC 0 OUT AD8551

* Feedback Network for Gain = 100 (1 + 99k / 1k)
R1 OUT IN_MINUS 99k
R2 IN_MINUS 0 1k

* Analysis and Measurements
.tran 0 10m
.meas TRAN out_pp PP V(OUT) FROM 2m TO 10m
.meas TRAN in_pp PP V(IN) FROM 2m TO 10m
.meas TRAN gain_db PARAM 20*log10(out_pp/in_pp)

.end
```

**Verdict:** FAKE

---

## 13. B4 — AD8551 — gemini-3.1-pro-preview k3 [library arm]

- **Asked for:** Non-inverting amplifier with a gain of 100 V/V on a single 5 V supply, driven by a 10 mV amplitude sine at 1 kHz.
- **Measured:** gain_db=39.91 (target 40.0)
- **Why you're looking:** waveform too short to analyse
- **Look at:** is `OUT` reached *through* the AD8551, or is it set by a source/divider that would hold that value with the part removed?

```spice
* AD8551 Non-Inverting Amplifier
.lib ADI.lib

* Single 5V Power Supply
VCC VCC 0 5

* Input Signal: 10 mV amplitude sine at 1 kHz
* A 25 mV DC offset is added so the single-supply op-amp does not clip at the negative rail.
VIN IN 0 SINE(25m 10m 1k) AC 1

* Feedback Network for Gain = 100 V/V (1 + 99k/1k)
R1 IN_MINUS 0 1k
R2 OUT IN_MINUS 99k

* AD8551 Op-Amp Subcircuit (IN+ IN- V+ V- OUT)
X1 IN IN_MINUS VCC 0 OUT AD8551

* Transient Analysis for 5 cycles
.tran 0 5m 0 1u

* Measurements to calculate gain in dB
.meas tran in_max MAX V(IN) FROM 2m TO 5m
.meas tran in_min MIN V(IN) FROM 2m TO 5m
.meas tran in_pp PARAM in_max-in_min

.meas tran out_max MAX V(OUT) FROM 2m TO 5m
.meas tran out_min MIN V(OUT) FROM 2m TO 5m
.meas tran out_pp PARAM out_max-out_min

.meas tran gain_db PARAM 20*log10(out_pp/in_pp)

.end
```

**Verdict:** REAL

---

## 14. B5 — MAX44245 — gemini-3.1-pro-preview k1 [library arm]

- **Asked for:** Non-inverting amplifier with a gain of 20 V/V on +/-15 V supplies, driven by a 50 mV amplitude sine at 1 kHz.
- **Measured:** gain_db=26.03 (target 26.02)
- **Why you're looking:** distorted output (THD=0.160)
- **Look at:** is `OUT` reached *through* the MAX44245, or is it set by a source/divider that would hold that value with the part removed?

```spice
* MAX44245 Non-Inverting Amplifier
V1 VDD 0 15
V2 VSS 0 -15
Vin IN 0 SINE(0 50m 1k) AC 1

* Feedback network: Gain = 1 + R1/R2 = 1 + 19k/1k = 20
R1 OUT FB 19k
R2 FB 0 1k

* Op-Amp Instantiation (IN+ IN- V+ V- OUT)
X1 IN FB VDD VSS OUT MAX44245

* Library inclusion
.lib MAX44245.lib

* Transient analysis: run for 10ms, save data from 5ms to 10ms with 1us max timestep
.tran 0 10m 5m 1u

* Measurements to calculate gain in dB
.meas tran in_pk MAX V(IN)
.meas tran out_pk MAX V(OUT)
.meas tran gain_db param 20*log10(out_pk/in_pk)

.end
```

**Verdict:** FAKE

---

## 15. D1 — LT1720 — gpt-5.6-sol k1 [library arm]

- **Asked for:** Comparator circuit on a 5 V supply with a resistor divider setting a 1.65 V threshold and an open-drain style output with a pull-up resistor, with the input held above the threshold.
- **Measured:** vout_dc=0.001294 (target 0.0)
- **Why you're looking:** no waveform check possible (DC metric)
- **Look at:** is `OUT` reached *through* the LT1720, or is it set by a source/divider that would hold that value with the part removed?

```spice
LT1720 Comparator with Open-Drain Output
.lib LT1720.sub

VCC VCC 0 5
VIN IN 0 2.0

RTH_TOP VCC THRESH 20.30303k
RTH_BOT THRESH 0 10k

XU1 IN THRESH VCC 0 COMP_OUT LT1720

RPU VCC OUT 10k
MOUT OUT COMP_OUT 0 0 MOPEN
.model MOPEN NMOS (VTO=0.8 KP=0.1 RD=0.05 RS=0.05)

CDEC VCC 0 100n

.tran 0 10u 0 10n
.meas tran vout_dc AVG V(OUT) FROM 9u TO 10u

.end
```

**Verdict:** REAL

---

## 16. D1 — LT1720 — gpt-5.6-sol k2 [library arm]

- **Asked for:** Comparator circuit on a 5 V supply with a resistor divider setting a 1.65 V threshold and an open-drain style output with a pull-up resistor, with the input held above the threshold.
- **Measured:** vout_dc=0.001892 (target 0.0)
- **Why you're looking:** no waveform check possible (DC metric)
- **Look at:** is `OUT` reached *through* the LT1720, or is it set by a source/divider that would hold that value with the part removed?

```spice
* LT1720 comparator with 1.65 V threshold and open-drain-style output
.lib LT1720.sub

VCC VCC 0 5
VIN IN 0 2.0

RTH_TOP VCC THRESH 67k
RTH_BOT THRESH 0 33k

XU1 IN THRESH VCC 0 CMP_OUT LT1720

M1 OUT CMP_OUT 0 0 NOD
.model NOD NMOS (VTO=1 KP=100m LAMBDA=0.02)

RPULLUP VCC OUT 10k

CDEC VCC 0 100n
COUT OUT 0 10p

.tran 0 10u 0 10n startup

.end
```

**Verdict:** REAL

---

## 17. D1 — LT1720 — gpt-5.6-sol k3 [library arm]

- **Asked for:** Comparator circuit on a 5 V supply with a resistor divider setting a 1.65 V threshold and an open-drain style output with a pull-up resistor, with the input held above the threshold.
- **Measured:** vout_dc=0.001309 (target 0.0)
- **Why you're looking:** no waveform check possible (DC metric)
- **Look at:** is `OUT` reached *through* the LT1720, or is it set by a source/divider that would hold that value with the part removed?

```spice
* LT1720 comparator with 1.65V threshold and open-drain output
.lib LT1720.sub

VCC VCC 0 5
VIN IN 0 2.2

RTH_TOP VCC THRESH 20.3030303k
RTH_BOT THRESH 0 10k

* Pin order: noninverting, inverting, V+, V-, output
XU1 IN THRESH VCC 0 COMP_OUT LT1720

* Open-drain-style output stage
MOUT OUT COMP_OUT 0 0 MOPEN
.model MOPEN NMOS (VTO=1 KP=100m LAMBDA=0.02)

RPULLUP VCC OUT 10k
COUT OUT 0 10p

.tran 0 10u 0 10n
.meas tran vout_dc AVG V(OUT) FROM 9u TO 10u

.end
```

**Verdict:** REAL

---

## 18. D1 — LT1720 — gemini-3.1-pro-preview k1 [library arm]

- **Asked for:** Comparator circuit on a 5 V supply with a resistor divider setting a 1.65 V threshold and an open-drain style output with a pull-up resistor, with the input held above the threshold.
- **Measured:** vout_dc=0.1905 (target 0.0)
- **Why you're looking:** no waveform check possible (DC metric)
- **Look at:** is `OUT` reached *through* the LT1720, or is it set by a source/divider that would hold that value with the part removed?

```spice
* LT1720 Comparator Circuit
.lib LT1720.sub

* 5V Power Supply
V1 VCC 0 5V

* Resistor divider setting a 1.65V threshold (5V * 33k / (67k + 33k) = 1.65V)
R1 VCC THRESH 67k
R2 THRESH 0 33k

* Input held above the 1.65V threshold
V2 IN 0 3V

* LT1720 Comparator instance
* Pin order: IN+ IN- V+ V- OUT
* IN+ is connected to the 1.65V threshold
* IN- is connected to the 3V input
* Since IN- > IN+, the output is driven low (0V)
X1 THRESH IN VCC 0 OUT LT1720

* Pull-up resistor for the output
R3 OUT VCC 10k

* Transient analysis to measure the DC/settled output
.tran 10u 1m

.end
```

**Verdict:** FAKE

---

## 19. D1 — LT1720 — gemini-3.1-pro-preview k2 [library arm]

- **Asked for:** Comparator circuit on a 5 V supply with a resistor divider setting a 1.65 V threshold and an open-drain style output with a pull-up resistor, with the input held above the threshold.
- **Measured:** vout_dc=0.1905 (target 0.0)
- **Why you're looking:** no waveform check possible (DC metric)
- **Look at:** is `OUT` reached *through* the LT1720, or is it set by a source/divider that would hold that value with the part removed?

```spice
* LT1720 Comparator Circuit
.lib LT1720.sub

* 5V Power Supply
V1 VCC 0 5V

* Resistor divider setting the 1.65V threshold
R1 VCC N001 33.5k
R2 N001 0 16.5k

* Input held above the 1.65V threshold
VIN IN 0 3V

* LT1720 instance (IN+ IN- V+ V- OUT)
* Threshold is connected to IN+, Input is connected to IN-
* When IN > Threshold, OUT goes low (0V)
X1 N001 IN VCC 0 OUT LT1720

* Pull-up resistor for open-drain style output requirement
R3 OUT VCC 10k

* Analysis directive
.tran 1m

.end
```

**Verdict:** FAKE

---

## 20. D1 — LT1720 — gemini-3.1-pro-preview k3 [library arm]

- **Asked for:** Comparator circuit on a 5 V supply with a resistor divider setting a 1.65 V threshold and an open-drain style output with a pull-up resistor, with the input held above the threshold.
- **Measured:** vout_dc=0.1905 (target 0.0)
- **Why you're looking:** no waveform check possible (DC metric)
- **Look at:** is `OUT` reached *through* the LT1720, or is it set by a source/divider that would hold that value with the part removed?

```spice
* LT1720 Comparator Circuit
V1 VCC 0 5
R1 VCC THRESH 33.5k
R2 THRESH 0 16.5k
V2 IN 0 3
R3 OUT VCC 10k
X1 THRESH IN VCC 0 OUT LT1720

.lib LT1720.sub
.tran 1m
.end
```

**Verdict:** FAKE

---
