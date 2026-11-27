- **The verify workflow caps the audit probe's memory on a hosted runner.** The
  kernel replay can fill a 16 GB hosted runner's memory, and a runner out of
  memory can stop answering before the kernel kills Lean; GitHub then reports
  that it lost the runner, with no log. The generated `autoform-verify.yml` now
  runs the probe in a cgroup capped at the runner's RAM less 2.5 GiB, so Lean
  swaps, or is killed inside the cap and the step says so. On a project whose
  replay fills the runner the cap cost no measurable time. Self-hosted runners
  are unchanged. A project set up before this release keeps its workflow; copy
  the probe lines of the Audit step from the template to take the cap.
