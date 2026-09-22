"use strict";

const { spawnSync } = require("node:child_process");

const suites = ["service_contract", "test_domain", "test_http_api"];

for (const suite of suites) {
  const result = spawnSync("python3", ["-m", "unittest", "-v", suite], { stdio: "inherit" });
  if (result.error) {
    console.error(result.error.message);
    process.exit(1);
  }
  if (result.status !== 0) {
    process.exit(result.status ?? 1);
  }
}

// 端到端试运行：长短内容、紧急回滚、事件乱序、按日复现、同口径对比
const trial = spawnSync("python3", ["trial.py"], { stdio: "inherit" });
if (trial.error) {
  console.error(trial.error.message);
  process.exit(1);
}
process.exit(trial.status ?? 1);
