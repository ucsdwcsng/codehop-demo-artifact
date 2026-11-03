# README

Demo artifact for CodeHop in Hotnets '26. We have simulated the full modulation-transmission-detection-decoding pipeline for CodeHop and made an interactive viewer that lets the user change parameters. The simulator compares 2 decoders, an interference-cancelling (SIC) decoder which is significantly more complex but can achieve higher throughput, and a less complicated traditional (TIN) decoder. The simulator also lets you test LoRa transmissions in the same scenario. In order to make the comparison more fair and bring out the advantage of CDMA over all nodes sharing the same (chirp) spreading sequence, we have added frequency hopping to LoRa as well.

# CodeHop Simulator reference

This document describes every option, readout and panel in the simulator page. The model is in `codehop_sim/uplink.py` and `codehop_sim/footprint.py`. The server and parameter bounds are in `server.py`.

## Contents

- [What one frame is](#what-one-frame-is)
- [Scope](#scope)
- [Buttons](#buttons)
- [Options](#options): [Waveform](#waveform) · [Nodes](#nodes) · [Power](#power) · [PHY](#phy) · [Packet](#packet) · [Receiver](#receiver) · [Run](#run) · [Satellite Parameters](#satellite-parameters)
- [Readouts](#readouts)
- [Panels](#panels): [Result tiles](#result-tiles) · [Spectrogram](#spectrogram) · [Scenario Map](#scenario-map) · [Correlation](#correlation) · [Doppler](#doppler-panel) · [Nodes table](#nodes-table) · [Sweep](#sweep)
- [Paper's Fig. 4a](#papers-fig-4a)

## What one frame is

A frame is one uplink burst from K nodes to one satellite receiver. The simulator computes each frame in five steps:

1. **Geometry.** The simulator puts the nodes in a circular region on the ground under a LEO pass. It puts the satellite at a chosen time in that pass. For every node, it computes slant range, elevation, Doppler, Doppler rate and propagation delay.
2. **Transmit.** Every node sends one packet of P pilot symbols followed by D data symbols. Each symbol is one cyclic shift of a base sequence. For CodeHop, the base sequence is the code of the node. For LoRa, it is a chirp that all nodes share. CodeHop moves every symbol to a new channel. LoRa keeps one channel for the whole packet.
3. **Channel.** The simulator shifts the signal of each node by its channel frequency, Doppler, Doppler rate and oscillator offset. It scales the signal to its SNR and delays it to its arrival time. It sums all signals into one wideband complex baseband capture. It then adds white Gaussian noise.
4. **Receive.** The receiver first finds the transmissions. For each one, it finds the code, the hop pattern, the start time and the carrier offset (see [Acquisition](#acquisition)). It then decodes every symbol twice on the same capture:
   - **TIN** (treat interference as noise) decides each symbol on its own;
   - **SIC** (ordered successive interference cancellation) decodes and subtracts symbols one at a time, strongest first.
5. **Score.** A data symbol is correct if the decided shift is equal to the transmitted shift. A packet is decoded if its number of wrong symbols is within the number that an RS(D, k) code can correct.

Frames are deterministic. The same settings and the same seed always give the same frame.

## Buttons

| Control | Action |
|---|---|
| Run | Simulates one frame with the current settings. |
| New seed | Picks a random seed and then runs. |
| Defaults | Restores every option to its default and then runs. |
| Auto | When checked, each change to an option runs the frame again after a delay of 0.45 s. |
| Theme (moon icon) | Switches between light and dark. The browser stores the choice. |

The status text at the top right shows the compute time of the frame. If a request fails, it shows the error.

## Options

The page hides the options that do not apply to the current settings. For example, it hides Code and Chip rate when Modulation is Hopping LoRa.

### Waveform

#### Modulation
Default: CodeHop. Choices: CodeHop, Hopping LoRa.

- **CodeHop.** Each node has its own spreading code from the selected code family. A symbol carries log2(N) bits as one of N cyclic shifts of that code, where N is the code length. The node sends chips with GFSK at modulation index 0.5 (MSK-like, constant envelope). The node sends each pilot on a channel from a public sequence that its code selects (see [Pilots](#pilots)). It sends each data symbol on the channel that its hop pattern gives. After each retune, the carrier phase is a new random value.
- **Hopping LoRa.** Every node uses the same up-chirp of N = 2^SF chips at a chip rate equal to the bandwidth. A symbol is a cyclic shift of that chirp. Each node stays on one channel for the whole packet (the first channel of its hop pattern) with continuous phase. Its pilots are plain unshifted up-chirps. The simulator inserts no guard. The name refers to the channel hop between packets, as in LoRaWAN. A node does not change channel inside a packet.

A dechirp followed by an FFT is the same computation as a correlation against all cyclic shifts. Therefore both modulations use the same receiver code.

When you change the modulation, the items that are not part of the waveform stay the same:
- node positions;
- shadowing, oscillator and arrival draws;
- hop pattern index;
- channel grid, packet length, receiver and seed.

The simulator draws only the data symbols again, because the alphabet size changes. For this reason, the result tiles can compare the two modulations on the same nodes.

In Link budget power mode, the SNR of LoRa is 10·log10(BW/Rc) lower than the SNR of CodeHop (0.97 dB for 125 kHz against 100 kcps). The noise bandwidth is wider. The received power is the same.

### Nodes

#### Simultaneous transmissions
Default: 32. Range: 1–128.

Number of nodes K that transmit in the frame. The receiver knows all of them.

#### Signatures
Default: Random. Choices: Random, Distinct. CodeHop only.

- **Random.** Each node picks its code uniformly from the family and its hop pattern uniformly from the pool. Each pick is independent of the other nodes. Two nodes can share a code, or a code and a hop pattern. The Nodes readouts give the probability.
- **Distinct.** The simulator draws codes without replacement, so no two nodes share a code. K cannot exceed the number of codes in the family. The simulator still draws hop patterns at random.

For Hopping LoRa, every node shares the chirp, so the page hides this option.

#### Arrival
Default: Beacon. Choices: Beacon, Aligned, Unslotted.

This option controls when each packet reaches the satellite. The simulator rounds the offsets to the sample grid of the capture. The capture starts at the earliest arrival that the geometry permits, and it is long enough for the latest arrival. Its length does not depend on the positions of the actual nodes, so it gives the receiver no information about them.

- **Beacon.** Every node transmits when it hears a beacon from the satellite. A node at slant range r arrives 2(r − r_min)/c after the closest node. This delay includes the downlink delay and the uplink delay. Across a 600 km region, the first and the last arrival are typically 4 to 5.5 ms apart.
- **Aligned.** Every packet starts at the same sample, so all packets overlap completely.
- **Unslotted.** Each node starts at a uniform random time in [0, Window], plus its one-way delay difference (r − r_min)/c.

#### Window
Default: 10 ms. Range: 0–50 ms. Shown for Unslotted arrival.

Width of the random start-time interval.

### Power

SNR is always defined per node in the chip-rate bandwidth: Rc for CodeHop, BW for LoRa. The receiver despreads N chips. This adds 10·log10(N) dB of processing gain to the SNR.

#### Model
Default: Link budget. Choices: Link budget, Equal, Near–far.

- **Link budget.** SNR = EIRP + Rx gain − FSPL(slant range, carrier) − shadowing − (−174 dBm/Hz + 10·log10(noise bandwidth) + Noise figure). The value for each node depends on its own slant range.
- **Equal.** Every node has the SNR that the SNR option gives.
- **Near–far.** The simulator draws the SNR of each node uniformly from [SNR, SNR + Spread].

#### SNR
Default: −6 dB. Range: −20 to 10 dB. Shown for Equal and Near–far.

Per-node SNR in Equal mode, or the weakest possible SNR in Near–far mode.

#### Spread
Default: 10 dB. Range: 0–30 dB. Shown for Near–far.

Width of the uniform SNR distribution.

#### EIRP
Default: 22 dBm. Range: 0–30 dBm. Shown for Link budget.

Node transmit power plus antenna gain. 22 dBm is the maximum output of an SX126x-class radio.

#### Rx gain
Default: 6 dBi. Range: −3 to 20 dBi. Shown for Link budget.

Satellite antenna gain. It is the same toward every node. The simulator models no beam pattern.

#### Noise figure
Default: 3 dB. Range: 0–10 dB. Shown for Link budget.

Satellite receiver noise figure.

#### Shadowing
Default: 4 dB. Range: 0–10 dB. Shown for Link budget.

Each node loses an extra |X| dB, where X is drawn from a normal distribution with this standard deviation. Shadowing is always a loss. It never increases the power of a node.

### PHY

#### Code
Default: Gold-127. CodeHop only.

The code family. Its code length N sets the symbol alphabet, bits per symbol, processing gain and symbol duration.

| Family | Length N | Codes | Bits per symbol | Processing gain |
|---|---|---|---|---|
| Gold-63 | 63 | 65 | 5.98 | 18.0 dB |
| Gold-127 | 127 | 129 | 6.99 | 21.0 dB |
| PRN-255 | 255 | 256 | 7.99 | 24.1 dB |
| Gold-511 | 511 | 513 | 8.99 | 27.1 dB |

PRN-255 codes are random ±1 sequences generated from a fixed public seed. Gold codes have a bounded cross-correlation guarantee. PRN-255 codes do not. Longer codes need more computation. The server rejects frames above its cost cap.

#### Chip rate
Default: 100 kcps. Range: 25–200 kcps. CodeHop only.

Chips per second. Symbol duration is N/Rc (1.27 ms for Gold-127 at 100 kcps). The occupied bandwidth and noise bandwidth scale with Rc. In Link budget mode, if you double Rc, the SNR of every node decreases by 3 dB.

#### Pulse Filter BT
Default: 0.5. Choices: None, 0.3, 0.5, 1.0. CodeHop only.

Bandwidth–time product of the Gaussian filter that shapes the GFSK frequency pulse. None uses rectangular pulses (plain CPFSK/MSK). A lower BT gives a narrower spectrum and more overlap between adjacent chips. The modulation index is fixed at 0.5.

#### SF
Default: SF7. Choices: SF7–SF12. Hopping LoRa only.

Spreading factor. Each symbol is N = 2^SF chips and carries SF bits. Each increase of SF by one doubles the symbol duration and adds 3 dB of processing gain.

#### Bandwidth
Default: 125 kHz. Choices: 125, 250, 500 kHz. Hopping LoRa only.

LoRa bandwidth. It is also the chip rate and the noise bandwidth.

#### Channels
Default: 8. Range: 1–16.

Number of channels on the grid. Channel c is centred at (c − (C − 1)/2) × Spacing from the carrier. With 1 channel every node transmits at the same frequency. Both modulations use the same grid.

#### Spacing
Default: 200 kHz. Range: 100–1000 kHz.

Distance between channel centres. If Spacing is smaller than the signal bandwidth, adjacent channels overlap.

#### Hop patterns
Default: 1024. Range: 1–1024. CodeHop only.

Size of the public pool of hop patterns. A pattern lists one channel per symbol, each drawn uniformly at random. The simulator makes the pool from a fixed public seed. Each node picks one pattern uniformly. CodeHop data symbols follow the pattern; CodeHop pilots follow the pilot channels of the code. With 1 pattern every node hops identically. LoRa nodes use the first channel of their pattern for the whole packet.

### Packet

#### Pilots
Default: 4. Range: 1–8.

Number of known symbols at the start of each packet.
- **CodeHop.** Pilot j uses the public shift (j·⌊N/4⌋ + j²) mod N. A public table gives each code its own sequence of pilot channels, and pilot j is sent on channel j of that sequence. Thus a receiver that does not know the active set can search each code on one channel per pilot. A false match between two codes also collects pilot energy only where their pilot channels are the same.
- **LoRa.** Pilots are unshifted up-chirps. They are the LoRa preamble.

The receiver uses the pilots to estimate the carrier offset. SIC cancels the pilots like any other symbol. The results do not include the pilots.

#### Data symbols
Default: 16. Range: 1–16.

Number of data symbols D per packet. Each symbol has a uniformly random shift. The simulator scores each symbol.

#### RS k
Default: 12. Range: 1–D.

The number k of information symbols of an illustrative Reed–Solomon code RS(D, k) over GF(p). Here p is the largest prime ≤ N (127 for Gold-127 and for SF7). A packet is decoded when it has at most ⌊(D − k)/2⌋ wrong data symbols. If k = D, the packet is uncoded and every symbol must be correct. The simulator does not do encoding or decoding.

#### Guard
Default: 0 µs. Range: 0–2000 µs. CodeHop only.

Silent gap after each symbol. It represents the synthesizer retune time. It makes the packet longer. It does not change the decoding. LoRa never retunes inside a packet, so LoRa ignores this option.

### Receiver

#### Acquisition
There is no option. The receiver finds the transmissions itself, in up to three search rounds. Each round has these steps:
1. **Channelize.** The receiver shifts the capture to each channel and decreases the rate to 2 samples per chip. When Doppler comp is on, it also removes the predicted Doppler.
2. **Search.** For each code, the receiver correlates the known pilots on the pilot channels of that code. It does this at every start time in the arrival window and every carrier offset within ± Search span. It adds the energies of the pilots.
3. **Verify.** For each candidate above the noise threshold, the receiver correlates every data block on every channel against all shifts of the code. It selects the hop pattern from the public pool that collects the most energy.
4. **Reject.** The receiver rejects a candidate if one pilot supplies most of the pilot energy. It also rejects a candidate if its data blocks have less energy than decoys: wrong codes at the same time and offset.
5. **Refine.** The receiver refines the start time to one sample and the carrier offset to a few hertz.
6. **Cancel.** SIC decodes and subtracts the nodes found so far. If the pilots of a found node have no energy after SIC, the receiver removes it, because it is an alias of a stronger node. The next round searches what remains.

The second and third rounds find weak nodes that stronger nodes hide. TIN uses only the nodes of the first round, because TIN cancels nothing. A node that the receiver does not find is lost, and all its symbols count as wrong. A false detection does not count as a packet.
For Hopping LoRa, the receiver searches each channel instead of each code, because all nodes share one chirp. Three items are different:
- There are no decoys. Each LoRa data symbol is the shared chirp at some offset, so data energy cannot identify a LoRa node. The pilot test does this work, as the several identical up-chirps of a real LoRa preamble do.
- On the up-chirp pilots, a delay of x chips has the same effect as an offset of x bins. The receiver therefore moves each node along that line to the point where the data blocks give the sharpest correlation peaks. A real LoRa packet has down-chirps for this purpose; the modelled packet does not.
- Two detections on one channel that lie on that line are one node.

#### Carrier offset
There is no option. The receiver estimates the carrier frequency offset of each node (Doppler + oscillator offset) from its pilots. The search gives a first estimate (step 2). The receiver then refines it in five steps for each pilot:
1. shifts the frequency of the pilot down by its channel frequency, and also by the predicted Doppler when Doppler comp is on;
2. multiplies by the conjugate of the known pilot waveform;
3. adds groups of adjacent samples, to decrease the rate to 4 samples per chip (integrate and dump);
4. takes a zero-padded FFT;
5. adds the squared FFT magnitudes over all pilots.

The estimate is the largest peak near the first estimate. The receiver refines it with parabolic interpolation.

#### Search span
Default: 25 kHz. Range: 1–40 kHz.

Half-width of the frequency search around the search centre. It must cover the offset of each node from the centre:
- the Doppler difference across the region, about ±17 kHz for a 600 km region at 550 km altitude;
- the oscillator error;
- the full bulk Doppler also, when Doppler comp is off.

A wider span gives the pilots of other nodes more room to make a false peak.

#### Doppler comp
Default: on.

The receiver knows the orbit of the satellite, so it can predict the Doppler and Doppler rate at the region centre.

When it is on, the receiver removes the predicted Doppler before the estimate. The search is then centred on the predicted Doppler. The receiver uses the predicted Doppler rate to track the frequency drift from the pilots to later symbols.

When it is off, the receiver centres the search at 0 Hz. It does not track drift. Near the horizon, the bulk Doppler (about ±20 kHz at 915 MHz) can then be outside the search span.

### Run

#### Seed
Default: 0. Range: 0 to 2³¹ − 1.

The seed sets every random draw:
- positions, signatures and hop patterns;
- data symbols and carrier phases;
- shadowing and oscillator offsets;
- arrival times;
- noise.

Each quantity has its own random stream. Thus, when you change one option, the simulator draws as few values again as possible.

### Satellite Parameters

The orbit is circular and the Earth is a sphere. The simulator ignores Earth rotation. The region centre is the point that sees the satellite at Peak elevation at closest approach.

#### Phase
Default: −0.34. Range: −0.98 to 0.98.

The position of the satellite in its pass, as a fraction of half the pass:
- −1 is the time when the satellite rises above Min elevation, as seen from the region centre;
- 0 is closest approach;
- +1 is the time when it sets.

The label shows rising, peak or setting. Negative values are before closest approach. The satellite then moves toward the region, so Doppler is positive. The Time readout gives the same position in seconds from closest approach.

#### Peak elevation
Default: 60°. Range: 15–90°.

Elevation of the satellite seen from the region centre at closest approach. At 90° the satellite passes directly overhead. Lower values give a pass that stays closer to the horizon, with longer slant ranges and a gentler Doppler curve.

#### Altitude
Default: 550 km. Range: 300–1200 km.

Orbit altitude. It sets orbital speed, path loss and the length of the pass.

#### Region radius
Default: 600 km. Range: 0–1200 km.

The simulator places the nodes uniformly by area in a circle of this ground radius around the region centre. At 0 km every node is at the same point, so all nodes have the same Doppler, delay and path loss. Larger regions give a wider spread of elevation, slant range, SNR, Doppler and arrival time.

#### Min elevation
Default: 10°. Range: 0–30°.

The simulator discards the nodes that see the satellite below this elevation at the current time. It draws new nodes for them. If no part of the region is above it, the frame fails with an error. This option also sets the length of the pass that Phase spans, and the outer ring on the Scenario Map.

#### Carrier
Default: 915 MHz. Choices: 433, 868, 915 MHz.

Carrier frequency. Doppler and oscillator offsets scale linearly with it. Free-space path loss grows as 20·log10(fc).

#### Oscillator error
Default: 2 ppm. Range: 0–20 ppm.

The local oscillator of each node has a constant frequency error. The simulator draws it uniformly from ±(ppm × carrier). At 2 ppm and 915 MHz, this is up to ±1.83 kHz. It adds to the Doppler of the node.

## Readouts

The grey boxes under each option group show values derived from the current settings. You do not need to run a frame to update them.

| Group | Readout | Meaning |
|---|---|---|
| Nodes | Code collision | Probability that at least two of the K nodes pick the same code: 1 − ∏ᵢ₌₀^(K−1) (1 − i/M), where M is the number of codes. Shown for Random signatures. |
| | Signature collision | The same formula with M = codes × hop patterns: the probability that two nodes share both code and hop pattern. |
| | Codes | Number of codes in the family. Shown for Distinct signatures. |
| | Channel collision | Hopping LoRa only: the same formula with M = channels. It is the probability that two nodes share a channel. |
| Power | Centre SNR | Link-budget SNR at the region centre, without shadowing. |
| PHY | Chips | Chips per symbol N. |
| | Symbol | Symbol duration T = N / chip rate. |
| | Bandwidth | Chip rate, which is also the noise bandwidth. |
| | Proc. gain | Processing gain 10·log10(N). |
| | CFO scale | 1/(4T): the offset at which a symbol's phase rotates by 90° from start to end. |
| | Sample rate | Capture sample rate, S × chip rate. S is the smallest multiple of 4 that makes the sample rate cover all channels, the signal bandwidth and the largest possible Doppler + oscillator offset. |
| Packet | Airtime | (Pilots + D) × (T + Guard). |
| | Payload | D × log2(N) raw bits. |
| | Correctable | ⌊(D − k)/2⌋ symbol errors per packet. |
| | Info bits | k × log2(p) for the RS code over GF(p). |
| Receiver | Search centre | Centre of the pilot search. It is the predicted Doppler when Doppler comp is on and 0 Hz when it is off. |
| | Doppler outside search | Warning: Doppler comp is off and the bulk Doppler is outside the search span. |
| Run | Cost | Estimated compute cost of one frame, including the search, as a percentage of the server's cap. |
| | Too heavy | Warning: the server will refuse this frame. Reduce nodes, code length, channels or symbols. |
| Satellite Parameters | Time | Seconds from closest approach. |
| | Elevation | Elevation of the satellite seen from the region centre. |
| | Slant range | Slant range from the region centre to the satellite. |
| | Doppler | Doppler at the region centre. |
| | Doppler rate | Rate of change of that Doppler, in Hz/s. |

## Panels

### Result tiles

The four tiles at the top give the results of this frame.

| Tile | Value | Second line |
|---|---|---|
| SIC symbols | Fraction of data symbols that SIC decoded correctly | Correct / total (K × D) |
| SIC packets | Packets SIC decoded | Nodes found out of K, and false detections |
| TIN symbols | Fraction of data symbols that TIN decoded correctly | Correct / total |
| TIN packets | Packets TIN decoded | Nodes found out of K, and false detections |

The bottom line of each tile shows the same metric for the other modulation on the same nodes. Every other setting is the same. The simulator computes it in the background after each run. The line shows "…" until the value arrives.

### Spectrogram

This panel shows the received capture as power over time and frequency offset from the carrier. It uses a Hann-window short-time Fourier transform. The simulator crops the frequency axis to the occupied band. The colour scale is jet.

| Control | Effect |
|---|---|
| Clean | Shows only the sum of the node signals, without receiver noise. This makes hops, Doppler offsets and overlaps easier to see. |
| Noisy | Shows the actual capture that the receiver uses, with the noise. |
| SIC / TIN | Selects the receiver whose decisions give the colour of the block outlines. It also applies to the Scenario Map colours and the Correlation panel. |
| Range | dB span of the colour scale below its top. The simulator sets it automatically on each run. It is 40 dB for Clean. For Noisy, it is enough to reach a little past the noise floor. |

Each outline is one symbol. It shows the time span of the symbol and ±0.6 Rc around its true frequency.

| Outline | Meaning |
|---|---|
| Green (Decoded) | The selected receiver found the right shift. |
| Red (Error) | It found the wrong shift. |
| White (Pilot) | Known pilot symbol. |
| Grey, dashed (Missed) | The selected receiver did not find this node. |

When you select a node, the page draws its outlines at full strength and dims the other outlines. A dashed box marks the block that the Correlation panel shows. When you click a block, the page selects the block and its node.

When you move the pointer over a block, the page shows these items:
- node, block index and channel;
- true shift and decision;
- decision margin (peak over runner-up, in dB);
- SIC step;
- node SNR and carrier offset.

The slider under the spectrogram selects a step of SIC.
- Position s shows the capture after the first s subtractions, in the order that SIC did them.
- The play button animates the subtractions.
- Received goes to the start. Residual goes to the end, after every symbol is subtracted.
- When you move the slider, the outlines change to SIC. The page fades the symbols that are already subtracted. It puts a box on the symbol that was subtracted last.

In Noisy view, the end state is mostly noise. In Clean view, the same subtractions apply to the noise-free signal. What remains is cancellation error. It has two causes: wrong decisions, and imperfect subtraction that carrier-offset error causes.

### Scenario Map

A top-down map of the region, the satellite and the nodes. The projection is azimuthal equidistant around the region centre, so distances from the centre are true ground distances. The horizontal axis is along the direction of travel of the satellite.

| Element | Meaning |
|---|---|
| Satellite icon | The point directly below the satellite at the current time. The arrow shows its direction of travel. |
| Ground track | The path of that point over the pass. It is solid where the satellite was and dashed where the satellite goes. |
| Grey rings | Contours of equal elevation angle to the satellite (Min elevation, 20°, 30°, 45°, 60°, 75°). Nodes inside a ring see the satellite at a higher elevation than the label of the ring. |
| Shaded area | The area where the satellite is above Min elevation. |
| Dashed circle | The node region (Region radius). |
| Dots | Nodes. The colour shows the fraction of the data symbols of the node that the selected receiver decoded: red at 0%, then orange and yellow, then green at 100%. |
| Dark ring around a dot | The packet of that node was not decoded. |
| Hollow red ring | The selected receiver did not find this node. |
| Scale bar | Ground distance. |

When you move the pointer over a node, the page shows:
- the SIC and TIN symbol counts and packet results of the node;
- SNR, elevation, slant range and Doppler;
- the code (CodeHop) or channel (LoRa) of the node.

When you click a node, the page selects it in the spectrogram and the Nodes table. It also opens the first data symbol of the node in the Correlation panel.

### Correlation

This panel shows the selected block, correlated against all N cyclic shifts, at the time when the receiver decided it.
- For SIC, this is after all earlier subtractions.
- For TIN, it is on the raw capture.
- For pilots, the panel always shows the SIC view.
- If the receiver did not find the node, the panel shows "Not detected".

The vertical axis is |correlation| normalised by block length. A node alone at SNR x dB peaks at 10^(x/20): 0.5 at −6 dB.

| Element | Meaning |
|---|---|
| Coloured bar and dot | The decided shift: green if correct, red if wrong. The label gives the shift. It also gives the true shift when the two shifts differ. |
| Dark bar | The true shift, when the decision was wrong. |
| Grey bars | All other shifts. They show leftover interference plus noise. |
| "noise" line | Expected level for noise alone, √(1/N). |

### Doppler panel

Doppler over the whole visible pass, against time from closest approach.

| Element | Meaning |
|---|---|
| Line | Doppler at the region centre. |
| Shaded band ("Nodes") | Range of Doppler across this frame's nodes at each time. |
| Vertical line and dot | Current time (Phase), with a label for the region-centre Doppler. |

### Nodes table

The table has one row for each node. When you click a row, the page selects the node. This is the same as a click on the node on the map.

| Column | Meaning |
|---|---|
| Node | Node index. |
| Code / Chirp | CodeHop: index of the code of the node in the family. LoRa: "shared". |
| Hop / Channel | CodeHop: index of the hop pattern of the node. LoRa: the channel of the node. |
| SNR dB | SNR in the chip-rate bandwidth. |
| Elev ° | Elevation of the satellite seen from the node. |
| Doppler kHz | Doppler at packet start. |
| LO Hz | Oscillator offset. |
| CFO err Hz | Estimated minus true carrier offset at the middle of the pilots. Empty if the SIC receiver did not find the node. |
| Arrival µs | Packet arrival time relative to the earliest node. |
| Found | Whether the SIC receiver found the node (✓ or ✗). |
| SIC, TIN | Data symbols correct out of D. |
| SIC pkt, TIN pkt | Whether the packet was decoded (✓ or ✗). |

### Sweep

The sweep runs many frames over a list of node counts. It plots recovery against K.

| Control | Effect |
|---|---|
| K grid | Comma-separated node counts, 1–128. |
| Frames | Frames per K, 1–20. Each frame has its own seed. The seed comes from the Seed option, K and the frame index. Thus a K value gives the same result for any other K values in the grid. |
| Run sweep | Starts the sweep with the current value of every other option. Points appear when each K finishes. |

The chart has four lines:
- solid lines for the symbol fraction under SIC (blue) and TIN (orange);
- dashed lines for the packet fraction under SIC and TIN.

The grey line marks 90%. When you move the pointer over the chart, it shows the values at the nearest K. Table lists the values.

If you change an option after a sweep, a Stale tag appears and the page dims the lines. This stays until you run the sweep again. The server refuses sweeps with a total cost above its cap.

## Paper's Fig. 4a

The paper evaluates a receiver that gets the set of active nodes, the code and exact timing of each node, and the carrier offset with Gaussian error (σ = 196.85 Hz). The page does not offer that receiver. The engine keeps it for the comparison: `demo/validate_paper_preset.py` in the CodeHop repository runs it and gets SIC symbol recovery of about 0.93 at K = 8 and 0.88 at K = 10, as in the paper.

These settings give the same nodes and channel on the page. The page receiver must find the nodes itself, so its results are not the paper's numbers.

| Option | Value |
|---|---|
| Modulation | CodeHop |
| Simultaneous transmissions | 8 or 10 |
| Signatures | Distinct |
| Arrival | Aligned |
| Region radius | 0 km |
| Oscillator error | 0 ppm |
| Model | Equal, SNR −6 dB |
| Code | Gold-127 |
| Chip rate | 100 kcps |
| Pulse Filter BT | 0.5 |
| Channels | 1 |
| Pilots / Data symbols | 4 / 8 |
| Doppler comp | On |
