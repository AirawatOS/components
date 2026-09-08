// One screen, in its own file — exported for app.js to import. Uses the shared primitives (AOS.page/table/form),
// so there is NO inline onclick and NO global function — which is what lets an app be split into modules at all.
let ROWS = [];

export function accountsScreen() {
  AOS.page('main',
    { title: 'Accounts', sub: 'this screen lives in screens/accounts.js', actions: [{ label: '＋ Add', primary: true, onClick: addAccount }] },
    AOS.table(ROWS, [
      { key: 'code', label: 'Code' },
      { key: 'name', label: 'Name', bold: true },
      { key: 'type', label: 'Type', fmt: 'pill' },
    ], { caption: 'Accounts (in-memory demo)', empty: 'No rows yet — click “＋ Add”.' }));
}

function addAccount() {
  AOS.form('New account', [
    { id: 'code', label: 'Code', required: true, placeholder: '5300' },
    { id: 'name', label: 'Name', required: true, placeholder: 'Utilities' },
    { id: 'type', label: 'Type', type: 'select', options: ['asset', 'expense', 'revenue', 'liability', 'equity'] },
  ], { onSave: v => { ROWS.push(v); accountsScreen(); AOS.toast('Added ' + v.code); } });
}
