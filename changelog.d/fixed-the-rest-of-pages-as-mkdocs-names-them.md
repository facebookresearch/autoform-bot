- **The rest of pages as MkDocs names them.** A file whose Markdown suffix is
  spelled in another case, such as `notes/Page.MD`, went through render as a
  page and was linked at `notes/Page.html`, which MkDocs never builds: it
  reads only a lower-case suffix as a page. It is now left out, with a warning
  naming the rename. Of two pages MkDocs would build at one URL, such as
  `index.md` beside `README.md`, MkDocs kept one with a warning that failed
  the strict build. The site now keeps an article's page, or a `README.md` it
  builds the landing page, the coverage view or the Sources entry from, and
  otherwise the page MkDocs keeps, and leaves the other out with a warning;
  two articles' pages both stay, with a warning. Two pages whose URLs differ
  only in case, which a disk that ignores case writes over each other, and a
  root README spelled otherwise, such as `readme.md` or `README.markdown`, now
  get a warning, and the landing page is built from a root `README.md` alone,
  on every disk (#670).
