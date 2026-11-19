- **A failed `links set` leaves what another call needs.** When the call's
  undo removed a template it had made, records another call had written below
  it since lost their defaults; the template now stays while such a record is
  there. When another call changed an article's own record, the undo had
  already removed the article's Mathlib record and restored its page, so it
  read no Mathlib declaration; the article's other files now stay too (#741,
  #748, #780).
