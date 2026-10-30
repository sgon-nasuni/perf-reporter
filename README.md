# Performance Report Publisher

Turn Jenkins performance test results into a clear report in Confluence. Include graphs from the latest run and, if you like, compare them with a baseline.

## Requirements

- Python 3.10 or later and Git
- Network/VPN access to Jenkins and Confluence
- Permission to read Jenkins build artifacts and run `Automation_Graphing`, and to create Confluence pages and upload attachments

## Install

```bash
git clone git@github.com:sgon-nasuni/perf-reporter.git
cd perf-reporter
python3 -m venv .venv
source .venv/bin/activate
python -m pip install PyYAML
```

## Credentials

- **Jenkins:** Open [Jenkins user settings](http://jenkins.perf.nasuni.net:8081/user/sgon/security/), then choose **API Token → Add new Token → Give a name →  Generate**.
- **Confluence:** [Create an Atlassian API token without scopes](https://id.atlassian.com/manage-profile/security/api-tokens) and use it with your Atlassian account email.

Paste the following into your terminal, replacing the example values. These variables apply to the current terminal session; keep the tokens private and out of the YAML file.

```bash
export JENKINS_USER="sgon"
export JENKINS_API_TOKEN="your-jenkins-api-token"
export CONFLUENCE_EMAIL="sgon@nasuni.com"
export CONFLUENCE_API_TOKEN="your-atlassian-api-token"
```

## Configure

Update [`control-report.yaml`](./control-report.yaml) with the Jenkins build, report title, Confluence destination, and test cases. Set `protocol` on each test suite; suites with the same protocol appear together in one page section. See the YAML file for the expected input format and optional baseline settings.

## Run

Choose a new output directory and a page title that does not already exist:

```bash
python jenkins_to_confluence.py \
  --config control-report.yaml \
  --output /tmp/performance-report \
  --allow-http
```