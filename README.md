# FIT Analysis Extractor

A command-line Python application that receives a `.fit` file and creates a folder named from the activity date/time and FIT filename:

```text
YYYY-MM-DD_HH-MM-SS_<FIT filename stem>/
```

Example:

```text
2026-07-19_08-17-35_Morning_Run/
├── full.json
└── analysis.json
```

The output deliberately contains only two JSON files:

- **`full.json`** preserves every decoded FIT message for archival and future reprocessing.
- **`analysis.json`** contains a compact, normalized structure for workout analysis.

The `.fit` (Flexible and Interoperable Data Transfer) file format was developed by Garmin and is primarily used to store and share health, fitness, and sports data—such as heart rate, GPS tracks, power, and cadence—recorded by smartwatches, cycling computers, and fitness applications. Garmin maintains the format and provides the official FIT SDK so developers can integrate decoding and encoding of `.fit` files into their own software. Because many other workout applications (such as Strava) also support the format, workout data downloaded from those services can be fed directly into this tool to extract data.

## Requirements

- Python 3.10 or newer
- `garmin-fit-sdk`

Install the dependency:

```bash
python -m pip install -r requirements.txt
```

## Basic usage

```bash
python fit_extractor.py Morning_Run.fit
```

By default, the output folder is created beside the FIT file.

Specify an output parent folder:

```bash
python fit_extractor.py Morning_Run.fit --output-root ./extracted
```

Specify the local timezone:

```bash
python fit_extractor.py Morning_Run.fit --timezone Europe/Budapest
```

Create a ZIP archive as well:

```bash
python fit_extractor.py Morning_Run.fit --zip
```

Replace an existing output folder:

```bash
python fit_extractor.py Morning_Run.fit --overwrite
```

Fail when the Garmin decoder reports any errors:

```bash
python fit_extractor.py Morning_Run.fit --strict
```

Disable CRC validation while decoding:

```bash
python fit_extractor.py Morning_Run.fit --no-crc
```

Install the project as a command:

```bash
python -m pip install .
fit-extract Morning_Run.fit
```

## `analysis.json`

The top-level structure is always:

```json
{
  "activity": {},
  "splits": [],
  "samples": [],
  "events": [],
  "context": {}
}
```

### Activity

Contains normalized activity-level values:

- source file name and sport
- local start and end timestamps
- distance, elapsed time, moving time, and explicit pause time
- average pace
- average and maximum heart rate
- average and maximum power
- minimum and maximum raw altitude
- calculated elevation gain and loss, plus calculation metadata
- calories and running strides

Elevation gain/loss is calculated from record altitude after:

1. filling missing altitude values by interpolation;
2. applying a centered 5-point median filter;
3. applying a centered moving average.

The centered moving-average window defaults to 15 seconds and can be changed:

```bash
fit-extract Morning_Run.fit --elevation-smoothing-window 21
```

The activity object records the calculation settings, for example:

```json
"elevation_calculation": {
  "source": "enhanced_altitude",
  "smoothing": "moving_average",
  "window_s": 15.0,
  "pre_filter": "median_5_samples"
}
```

For running activities, the FIT `total_strides` value is exposed as `strides`. It is not relabeled as steps.

### Splits

Splits are generated from cumulative record distance, not from manual FIT laps. The default split length is 1,000 metres. Boundary times and values are interpolated at the exact distance crossing.

Each split contains:

- start, end, and covered distance
- duration and pace
- average/maximum heart rate
- average/maximum power
- smoothed start/end altitude
- ascent, descent, and net elevation change

The final split may be shorter than one kilometre.

Change split length:

```bash
fit-extract Morning_Run.fit --split-distance 500
```

### Samples

The extractor merges FIT record messages that share a timestamp and interpolates sparse cumulative distance. `analysis.json` then contains regular time-based samples every 5 seconds by default. The precise activity endpoint is always appended, even when it does not fall on the sampling interval. It uses the final cumulative distance and the latest available value for any measurement omitted by a partial final FIT record.

Fields:

```text
elapsed_time_s
distance_m
speed_m_s
heart_rate_bpm
power_w
altitude_m
latitude
longitude
```

Change the interval:

```bash
fit-extract Morning_Run.fit --sample-interval 10
```

Optionally include GPS accuracy:

```bash
fit-extract Morning_Run.fit --include-gps-accuracy
```

### Events

FIT event and lap messages are normalized to:

```text
start
stop
pause
resume
lap
```

Duplicate events are removed, manual markers are converted to `lap`, and a final stop is stored at the precise activity duration.

## Adding context

Context does not normally exist in a FIT file. Supply it using a JSON file:

```bash
fit-extract Morning_Run.fit --context context.example.json
```

The file may contain the context object directly, as shown in `context.example.json`, or an object with a top-level `context` key.

The extractor derives hydration metrics as the required inputs become available:

- `body_mass_loss_pct` needs `weight_before_kg` and `weight_after_kg`.
- `estimated_sweat_loss_l` additionally needs `fluid_during_ml`.
- `sweat_rate_l_per_hour` additionally needs a positive moving time.

Calculations:

```text
body mass loss % = (weight before - weight after) / weight before × 100
estimated sweat loss L = weight loss kg + fluid during L - urine during L
sweat rate L/h = estimated sweat loss L / moving time hours
```

A missing urine value is treated as zero only when the other required hydration values are available.

Unknown context fields are rejected so misspellings do not silently enter the analysis schema.

## `full.json`

`full.json` contains:

- source filename, path, size, and SHA-256
- extraction time, timezone resolution, CRC setting, and decoder errors
- message counts
- every decoded Garmin FIT SDK message under its original message-type key

No CSV aliases or duplicate singular/plural exports are generated.

## Complete command help

```bash
fit-extract --help
```
