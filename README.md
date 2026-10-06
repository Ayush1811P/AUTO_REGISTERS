# Registration QA runner

Use this runner only with a site you own or are authorized to test, and test
numbers explicitly reserved for that site.

## Windows setup

From this folder, create the environment and install its dependencies:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m playwright install chromium
```

## Run

Configure the authorized staging host and its reserved test-number range:

```powershell
.\.venv\Scripts\python.exe bot.py --setup
```

Then start a run against that staging site. `--bundled` selects the Chromium
installed above instead of requiring Google Chrome:

```powershell
.\.venv\Scripts\python.exe bot.py --url "https://staging.example.test/register" --iterations 1 --bundled
```

Review the generated output under `results\`. The runner stops on ambiguous
results and does not automatically retry submissions.
