- **Progress from the impact commands.** `work impact` and `work import-impact`
  name each phase on stderr as it starts, from reading the roadmap through
  checking the build and running the impact probe to computing the impact, and
  a run that succeeds ends with one stderr line of the seconds each phase took.
  A long run on a large project no longer says nothing until it ends. Standard
  output is unchanged (#705).
