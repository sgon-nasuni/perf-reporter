# Jenkins to Confluence

Downloads Jenkins graphs, generates optional baseline comparisons, and publishes
a report inside your Confluence folder. No perf-automation installation needed.

## Setup

Requires Python 3.10+ and internal network/VPN access. From this folder:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install PyYAML
```

## API tokens

- **Jenkins:** [Security](http://jenkins.perf.nasuni.net:8081/user/sgon/security/)
  > API Token > Add new Token > Generate. Needs job/artifact read access and
  permission to build `Automation_Graphing`. [Help](https://www.jenkins.io/doc/book/system-administration/authenticating-scripted-clients/).
- **Confluence:** [Create a token without scopes](https://id.atlassian.com/manage-profile/security/api-tokens).
  Needs permission to create pages/upload attachments in the folder.
  [Help](https://support.atlassian.com/atlassian-account/docs/manage-api-tokens-for-your-atlassian-account/).

## Run

Edit [the YAML](performance_report.example.yaml): build URL, unique title, folder
URL, system/capability/protocol, and testcases. Omit `baseline` for current-only;
provide a UUID for comparison. FC64K: set testcase `axes: time_vs_create_rate`.

```bash
python jenkins_to_confluence.py --config performance_report.example.yaml \
  --output /tmp/performance-report --allow-http --workers 4
```

Enter Jenkins username/token and Atlassian email/token when prompted.
`--allow-http` sends the Jenkins token unencrypted: approved networks only.
Use a fresh output directory; successful publication deletes it, failures retain it.
`--prepare-only` keeps graphs without publishing (comparison jobs still run).
Upload only these three files to a **private** repository; never tokens or `.venv`.
