- **A level of a local graph that adds nothing can't be chosen.** The levels
  reach further back into what a result builds on, while the column of what
  it feeds into stays the same, so a result that builds on nothing, or whose
  whole ancestry level 1 already draws, showed the same graph at every level.
  A level that draws nothing more than the one below it is now disabled, with
  a title saying so; where it is the level chosen, the highest level below it
  that adds something is pressed, and pressing it makes it the choice. A graph
  where no level adds anything shows no levels, and its count says why. A level
  that adds only hub chips still counts (#793, #797).
