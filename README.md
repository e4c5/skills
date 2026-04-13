# Gemini CLI Skills

A collection of skills designed to extend the capabilities of various coding agents for common software engineering workflows.

## Prerequisites

### GitHub Integration

## Available Skills

### [Respond PR Review Comments](./respond-pr-review-comments)
Automates the analysis of pull request review comments. This skill identifies actionable findings, resolves non-actionable threads with a detailed reply, and generates a concrete implementation plan in a `fixes.md` file for any code changes required. Currently geared towards github PRs.

This skill requires that the [GitHub CLI (`gh`)](https://cli.github.com/) to be installed and authenticated. Run `gh auth login` to ensure you have the necessary permissions to read PRs and post review comments.

### [Analyze Sonar Issues](./analyze-sonar-issues)
Fetches and prioritizes SonarQube or SonarCloud issues and duplication reports. It discovers Sonar links from pull requests, fetches issues via the Sonar API, and builds a remediation plan to improve code quality and address technical debt.

For private projects, the `Analyze Sonar Issues` skill requires an authentication token. Set the `SONAR_TOKEN` (or `SONARCLOUD_TOKEN`) environment variable with your project's analysis token.

### [Find Last Good Commit](./find-last-good-commit)
Identifies the most recent commit in a git repository where a specified test suite passes. It automates the process of stashing uncommitted changes, iterating through the commit history, and running test commands to find a stable point for regression analysis.
