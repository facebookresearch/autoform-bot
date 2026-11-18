"""The reader's child indexes and native section hashes, with small DOM adapters, not browser substitutes."""

from __future__ import annotations

import json

import pytest

from tests.ui.test_integrated_navigation import _NODE, _run
from tests.ui.test_qa_outline_navigation import _run as _run_book

pytestmark = pytest.mark.skipif(_NODE is None, reason="Node is needed for isolated reader tests")

_INDEXES = r"""
// A reading's elements: their tags, attributes, classes and siblings, as markIndexes reads them.
class Tag {
  constructor(tag,attrs={},children=[]) {
    this.tagName=tag.toUpperCase();this.attrs=attrs;this.children=children;this.classes=new Set();
    this.classList={add:name=>this.classes.add(name)};children.forEach(child=>{child.parent=this;});
  }
  get id() {return this.attrs.id||'';}
  getAttribute(name) {return name in this.attrs?this.attrs[name]:null;}
  get nextElementSibling() {const siblings=this.parent?this.parent.children:[];return siblings[siblings.indexOf(this)+1]||null;}
  all() {return this.children.flatMap(child=>[child,...child.all()]);}
  querySelectorAll(selector) {
    const tags=selector.split(',').map(tag=>tag.toUpperCase());return this.all().filter(node=>tags.includes(node.tagName));
  }
}
const t=(tag,attrs,...children)=>new Tag(tag,attrs,children);
const list=(tag='ul')=>t(tag,{},t('li',{},t('a',{href:'topic/index.html'})));
function hidden(named,...children) {
  const reading=t('div',named===null?{}:{'data-reader-indexes':named},...children);markIndexes(reading);
  return reading.all().filter(node=>node.classes.has('bp-reader-index')).map(node=>node.id||node.tagName);
}
"""


def test_the_book_hides_each_child_index_render_names_with_the_lists_under_it() -> None:
    """The book marks each heading render names on the reading, with the lists under it, at any depth, and only where
    the lists are all the heading's section holds, up to the next heading of its level or above or the end."""
    _run_book(r"""
      assert.deepEqual(hidden('topics',t('h1',{id:'title'}),t('p',{}),t('h2',{id:'topics'}),list(),list('ol'),
        t('h2',{id:'notes'}),t('p',{})),['topics','UL','OL']);
      assert.deepEqual(hidden('topics more',t('div',{},t('h3',{id:'more'}),list()),t('h2',{id:'topics'}),list()),
        ['more','UL','topics','UL']);
      assert.deepEqual(hidden('topics',t('h3',{id:'topics'}),list(),t('h2',{id:'next'})),['topics','UL']);
      // A section holding more than lists, or none, is not a child index, whatever the attribute says.
      assert.deepEqual(hidden('topics',t('h2',{id:'topics'}),list(),t('p',{})),[]);
      assert.deepEqual(hidden('topics',t('h2',{id:'topics'}),list(),t('h3',{id:'deeper'}),list()),[]);
      assert.deepEqual(hidden('topics',t('h2',{id:'topics'}),t('h2',{id:'next'})),[]);
      // Only named headings, and a heading without an ID is never named.
      assert.deepEqual(hidden('notes',t('h2',{id:'topics'}),list()),[]);
      assert.deepEqual(hidden('',t('h2',{}),list()),[]);
      assert.deepEqual(hidden(null,t('h2',{id:'topics'}),list()),[]);
    """, ["markIndexes"], _INDEXES)


@pytest.mark.parametrize("hash_value", [
    "view=list&status=fully_proved", "isolates=0", "view=graph&direction=both&isolates=1",
    "direction=prerequisites&status=defined,conditional,inventory_checked", "status=-",
    "node=a&view=list&direction=dependents",
])
def test_recognized_legacy_filter_hashes_without_a_node_survive_cold_reload(hash_value: str) -> None:
    _run(f"""
      const hash={json.dumps(hash_value)},url=rootSpec.route+'#'+hash;
      const expected=Object.fromEntries(new URLSearchParams(hash));
      for(let reload=0;reload<2;reload++) {{
        const app=setup({{url}});await app.nav.start();
        const bundle=app.commits.at(-1);assert(bundle);
        assert.deepEqual(bundle.parameters,expected);assert.equal(bundle.anchor,expected.node?'theorem-a':null);
        assert.equal(app.errors.length,0);app.nav.dispose();
      }}
    """)


@pytest.mark.parametrize("hash_value", [
    "retained-section", "view=authored", "status=an_authored_heading", "status=fully_proved,",
    "view=list&heading=statement", "view=list&view=graph", "isolates=maybe", "direction=sideways",
])
def test_unknown_or_mixed_hashes_remain_native_sections_not_legacy_filter_state(hash_value: str) -> None:
    _run(f"""
      const hash={json.dumps(hash_value)},app=setup({{url:rootSpec.route+'#'+hash}});await app.nav.start();
      const bundle=app.commits.at(-1);assert(bundle);
      assert.deepEqual(bundle.parameters,{{}});assert.equal(bundle.anchor,decodeURIComponent(hash));
      assert.equal(bundle.focus,false);assert.equal(app.errors.length,0);app.nav.dispose();
    """)


def test_filter_only_hash_links_and_back_use_the_controller_while_native_section_links_do_not() -> None:
    _run(r"""
      const app=setup();await app.nav.start();
      const source=app.initial.reading;
      function click(hash) {
        const anchor=new Element('a',{href:rootSpec.route+'#'+hash},[],'A genuine internal link');
        source.children.push(anchor);anchor.parent=source;
        const event={target:anchor,button:0,defaultPrevented:false,preventDefault(){this.defaultPrevented=true;},stopPropagation(){}};
        source.listeners.get('click')(event);return event.defaultPrevented;
      }
      assert.equal(click('view=list&status=-'),true);
      await until(()=>app.commits.length===2);
      assert.deepEqual(app.commits.at(-1).parameters,{view:'list',status:'-'});assert.equal(app.commits.at(-1).anchor,null);
      assert.equal(click('isolates=0'),true);await until(()=>app.commits.length===3);
      assert.deepEqual(app.commits.at(-1).parameters,{isolates:'0'});
      app.win.history.back();await until(()=>app.commits.length===4);
      assert.equal(app.commits.at(-1).reason,'popstate');
      assert.deepEqual(app.commits.at(-1).parameters,{view:'list',status:'-'});
      assert.equal(app.win.location.href,rootSpec.route+'#view=list&status=-');
      const before=app.commits.length,requests=app.fetches.length;
      assert.equal(click('retained-section'),false);assert.equal(click('view=list&heading=authored'),false);
      app.win.history.pushState({},'',rootSpec.route+'#retained-section');app.listeners.get('popstate')();
      assert.equal(app.commits.length,before);assert.equal(app.fetches.length,requests);
      assert.equal(app.commits.at(-1).anchor,'retained-section');assert.equal(app.errors.length,0);app.nav.dispose();
    """)
