"""Production reader projection with small DOM adapters, not browser substitutes."""

from __future__ import annotations

import pytest

from tests.test_integrated_navigation import _NODE, _run

pytestmark = pytest.mark.skipif(_NODE is None, reason="Node is needed for isolated reader tests")

_SETUP = r"""
// Supply standard sibling/hidden/attribute DOM behavior to the existing router adapter.
Object.defineProperties(Element.prototype, {
  parentElement: {get() { return this.parent || null; }},
  nextElementSibling: {get() { return this.parent && this.parent.children[this.parent.children.indexOf(this)+1] || null; }},
  previousElementSibling: {get() { return this.parent && this.parent.children[this.parent.children.indexOf(this)-1] || null; }},
  id: {get() { return this.attrs.id || ''; }},
  hidden: {get() { return this.hasAttribute('hidden'); }, set(value) { if(value)this.setAttribute('hidden','');else this.removeAttribute('hidden'); }}
});
const matches = Element.prototype.matches;
Element.prototype.matches = function(selector) {
  if(selector.includes(',')) return selector.split(',').some(part=>this.matches(part.trim()));
  const attribute = /^\[([\w-]+)(?:=['"]([^'"]+)['"])?\]$/.exec(selector);
  if(attribute) return attribute[2] === undefined ? this.hasAttribute(attribute[1]) : this.getAttribute(attribute[1]) === attribute[2];
  return matches.call(this,selector);
};
const app=setup(), catalog=index();
catalog.nodes.push(...['topic-a','topic-b','scope:authored'].map(id=>({
  id,title:id,kind:'scope',result:false,scope:id,parent:'roadmap',navigation:'module',
  route:'../roadmap/'+id+'.html',data_url:'../dependencies.json'
})));
PRELOAD_MUTATION
app.contents.set(absolute('dependencies/index.json'),catalog);
await app.nav.start();
function item(id) { return new Element('li',{},[new Element('a',{href:absolute('roadmap/'+id+'.html')},[],id)]); }
const heading=new Element('h2',{id:'an-authored-catalog'},[],'Any authored catalogue title');
const list=new Element('ul',{},[item('topic-a'),item('topic-b')]);
const original=new Element('div',{'class':'bp-reading-source','data-autoform-node-id':'roadmap'},[
  new Element('h1',{id:'original-title'},[],'Root'), new Element('p',{},[],'Keep the complete mathematical exposition.'),
  heading,list,new Element('nav',{'class':'bp-book-nav'},[],'Keep the original chapter navigation.')
]);
const reading=original.cloneNode(true), body=new Element('article',{'class':'bp-reading-body'},[reading]);
app.initial.host.children.push(body); body.parent=app.initial.host; app.nav.bindReader(body);
const projectedHeading=reading.querySelector('h2'), projectedList=reading.querySelector('ul');
const represented=['topic-a','topic-b','scope:authored'];
"""


def _exercise(body: str, preload: str = "") -> None:
    _run(_SETUP.replace("PRELOAD_MUTATION", preload) + body)


def test_projection_hides_only_complete_represented_child_index_and_never_changes_ssr_cache_or_history() -> None:
    _exercise(r"""
      const history=JSON.stringify(app.win.history.state), indexBefore=JSON.stringify(app.nav.getIndex()), requests=app.fetches.length;
      assert.equal(app.nav.declutterReader(body,represented),1);
      assert.equal(projectedHeading.hidden,true); assert.equal(projectedList.hidden,true);
      assert.equal(projectedHeading.id,'an-authored-catalog'); assert.equal(projectedList.querySelectorAll('a').length,2);
      assert.equal(original.querySelector('h2').hidden,false); assert.equal(original.querySelector('ul').hidden,false);
      assert(reading.textContent.includes('Keep the complete mathematical exposition.'));
      assert(reading.textContent.includes('Keep the original chapter navigation.'));
      assert.equal(app.nav.declutterReader(original,represented),0); // Never mutate the SSR source.
      assert.equal(app.nav.declutterReader(body,represented),1);
      assert.equal(JSON.stringify(app.win.history.state),history); assert.equal(JSON.stringify(app.nav.getIndex()),indexBefore);
      assert.equal(app.fetches.length,requests); app.nav.dispose();
    """)


def test_incomplete_or_missing_outline_and_disposal_restore_only_this_instances_hiding() -> None:
    _exercise(r"""
      assert.equal(app.nav.declutterReader(body,represented),1);
      assert.equal(app.nav.declutterReader(body,['topic-a']),0);
      assert.equal(projectedHeading.hidden,false); assert.equal(projectedList.hidden,false);
      assert.equal(app.nav.declutterReader(body,represented),1);
      assert.equal(app.nav.declutterReader(body),0); assert.equal(projectedList.hidden,false);
      projectedList.hidden=true; // The source author's existing hidden state is not ours to undo.
      assert.equal(app.nav.declutterReader(body,represented),0); assert.equal(projectedList.hidden,true);
      projectedList.hidden=false; assert.equal(app.nav.declutterReader(body,represented),1);
      app.nav.dispose(); assert.equal(projectedHeading.hidden,false); assert.equal(projectedList.hidden,false);
    """)


@pytest.mark.parametrize("scenario", ["mixed", "prose", "sources", "bibliography", "proof", "math", "native_anchor", "external", "source_destination", "narrative_leaf"])
def test_source_proof_math_mixed_and_noncanonical_sections_stay_complete(scenario: str) -> None:
    mutation = {
        "mixed": "projectedList.children[0].text='Mathematical explanation outside the link.';",
        "prose": "const prose=new Element('p',{},[],'The listed objects are mathematical hypotheses.'); reading.children.splice(3,0,prose);prose.parent=reading;",
        "sources": "projectedHeading.setAttribute('id','sources');",
        "bibliography": "const wrapper=new Element('section',{role:'doc-bibliography'},[projectedHeading,projectedList]);reading.children.splice(2,2,wrapper);wrapper.parent=reading;",
        "proof": "projectedHeading.setAttribute('id','proof-depends-on');",
        "math": "const formula=new Element('span',{class:'arithmatex'},[],'\\(x=0\\)');projectedList.children[0].children[0].children.push(formula);formula.parent=projectedList.children[0].children[0];",
        "native_anchor": "projectedList.children[0].children[0].attrs.href+='#retained-formula';",
        "external": "projectedList.children[0].children[0].attrs.href='https://outside.test/reference.html';",
        "source_destination": "app.nav.getIndex().nodes.find(node=>node.id==='topic-a').navigation='source';",
        "narrative_leaf": "const leaf=app.nav.getIndex().nodes.find(node=>node.id==='topic-a');leaf.kind='node';leaf.scope='roadmap';leaf.result=false;",
    }[scenario]
    _exercise(mutation + r"""
      const before=reading.textContent;
      assert.equal(app.nav.declutterReader(body,represented),0);
      assert.equal(projectedHeading.hidden,false); assert.equal(projectedList.hidden,false);
      assert.equal(reading.textContent,before); app.nav.dispose();
    """)


def test_source_pages_leaf_links_and_missing_parent_evidence_are_not_scope_indexes() -> None:
    for mutation in (
        "catalog.nodes[0].navigation='source';",
        "catalog.nodes.find(node=>node.id==='topic-a').result=true;",
        "delete catalog.nodes.find(node=>node.id==='topic-a').parent;",
    ):
        _exercise(r"""
          assert.equal(app.nav.declutterReader(body,represented),0);
          assert.equal(projectedHeading.hidden,false); assert.equal(projectedList.hidden,false);
          app.nav.dispose();
        """, mutation)


def test_exact_authored_prefix_ids_and_loose_ordered_child_index_are_supported() -> None:
    _exercise(r"""
      projectedList.tagName='OL'; projectedList.children=[item('scope:authored')];projectedList.children[0].parent=projectedList;
      const entry=projectedList.children[0], anchor=entry.children[0], paragraph=new Element('p',{},[anchor]);
      entry.children=[paragraph];paragraph.parent=entry;
      assert.equal(app.nav.declutterReader(body,['scope:authored']),1);
      assert.equal(projectedList.hidden,true); assert.equal(projectedList.querySelector('a').href,absolute('roadmap/scope:authored.html'));
      app.nav.dispose();
    """)
