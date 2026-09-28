/* Progressive enhancement. Existing server permissions and forms remain authoritative. */
(() => {
  const nav = document.querySelector('.sidebar-menu');
  if (nav) {
    const member = nav.dataset.memberView === 'true';
    const groups = member ? [
      ['My money', ['My Savings', 'My Loans', 'Target Advance', 'Transactions', 'Apply for Loan', 'Guarantor Requests']],
      ['My documents', ['Statement', 'Events', 'Minutes']],
      ['Help and account', ['My Profile', 'Support', 'Security & 2FA', 'Training', 'Help']]
    ] : [
      ['Members and lending', ['Members', 'Savings', 'Savings Requests', 'Loans', 'Investments', 'Target Advance']],
      ['Payments and accounting', ['Receive Payment', 'Account Numbers', 'Accounting', 'Chart of Accounts', 'Journal Register', 'Bank Accounts', 'Reconciliation', 'Vouchers', 'Dividends', 'Reports', 'Upload History']],
      ['Communications and governance', ['Communications', 'Events & Minutes']],
      ['Administration', ['Settings', 'Task Assignment', 'Data Migration', 'Feedback', 'Leads', 'Billing', 'Affiliates']],
      ['Help and account', ['Security & 2FA', 'Training', 'Help']]
    ];
    const links = [...nav.querySelectorAll(':scope > a')];
    const first = links.find(a => groups.some(([,names]) => names.some(n => a.textContent.trim().startsWith(n))));
    const anchor = document.createComment('Navigation groups');
    nav.insertBefore(anchor, first || null);
    groups.forEach(([title, names]) => {
      const children = links.filter(a => names.some(n => a.textContent.trim() === n || a.textContent.trim().startsWith(n + '\n')));
      if (!children.length) return;
      const section = document.createElement('details'); section.className = 'nav-group';
      const summary = document.createElement('summary'); summary.textContent = title; section.append(summary);
      nav.insertBefore(section, anchor);
      children.forEach(a => section.append(a));
      section.open = children.some(a => a.classList.contains('active'));
    });
    nav.querySelectorAll('a.active').forEach(a => a.setAttribute('aria-current', 'page'));
    const exact=[...nav.querySelectorAll('a')].filter(a=>new URL(a.href).pathname===location.pathname);
    if(exact.length) {
      nav.querySelectorAll('a.active').forEach(a=>{a.classList.remove('active');a.removeAttribute('aria-current');});
      exact[0].classList.add('active'); exact[0].setAttribute('aria-current','page');
      const group=exact[0].closest('details'); if(group) group.open=true;
    }
  }
  const sidebar = document.getElementById('sidebar'), toggle = document.getElementById('sidebarToggle');
  if (sidebar && toggle) {
    new MutationObserver(() => toggle.setAttribute('aria-expanded', String(sidebar.classList.contains('show')))).observe(sidebar, {attributes: true, attributeFilter: ['class']});
    document.addEventListener('keydown', e => { if (e.key === 'Escape' && sidebar.classList.contains('show')) { sidebar.classList.remove('show'); toggle.focus(); } });
    document.addEventListener('keydown', e => {
      if(e.key!=='Tab'||!sidebar.classList.contains('show')||innerWidth>=768) return;
      const items=[toggle,...sidebar.querySelectorAll('a,button,summary,input')].filter(el=>!el.disabled&&el.getClientRects().length);
      const first=items[0],last=items[items.length-1];
      if(e.shiftKey&&document.activeElement===first){e.preventDefault();last.focus();}
      else if(!e.shiftKey&&document.activeElement===last){e.preventDefault();first.focus();}
    });
  }
  document.querySelectorAll('#pageContent label:not([for])').forEach((label,index)=>{
    const controls=label.parentElement.querySelectorAll('input:not([type=hidden]),select,textarea');
    if(controls.length!==1) return;
    const control=controls[0];
    if(!control.id) control.id='workspace-control-'+index;
    label.htmlFor=control.id;
  });
  document.querySelectorAll('button[title],a[title]').forEach(el=>{
    if(!el.textContent.trim()&&!el.hasAttribute('aria-label')) el.setAttribute('aria-label',el.title);
  });
  document.querySelectorAll('#pageContent .table-responsive').forEach((region, index) => {
    region.tabIndex = 0; region.setAttribute('role', 'region'); region.setAttribute('aria-label', 'Scrollable data table');
    const table = region.querySelector('table'), body = table?.tBodies[0];
    if (!body || table.querySelector('input:not([type=hidden]):not([type=checkbox]), select, textarea') || body.rows.length < 5) return;
    const rows = [...body.rows];
    if (rows.some(r => [...r.cells].some(c => c.colSpan > 1 || c.rowSpan > 1))) return;
    const tools = document.createElement('div'); tools.className = 'table-tools';
    const label = document.createElement('label'); label.textContent = 'Find in loaded rows';
    const search = document.createElement('input'); search.type = 'search'; search.className = 'form-control'; label.append(search);
    const status = document.createElement('small'); status.setAttribute('role', 'status');
    tools.append(label, status); region.before(tools);
    function filter() { let count = 0; rows.forEach(r => { r.hidden = !r.textContent.toLowerCase().includes(search.value.toLowerCase()); if (!r.hidden) count++; }); status.textContent = `${count} of ${rows.length} loaded rows`; }
    search.addEventListener('input', filter); filter();
    [...(table.tHead?.rows[0]?.cells || [])].forEach((th, col) => {
      if (th.querySelector('a, button, input') || th.colSpan > 1 || /action/i.test(th.textContent)) return;
      const button = document.createElement('button'); button.type = 'button'; button.className = 'table-sort'; button.textContent = th.textContent;
      th.replaceChildren(button); th.setAttribute('aria-sort', 'none');
      button.onclick = () => {
        const asc = th.getAttribute('aria-sort') !== 'ascending';
        table.querySelectorAll('[aria-sort]').forEach(h => h.setAttribute('aria-sort', 'none'));
        th.setAttribute('aria-sort', asc ? 'ascending' : 'descending');
        const value = r => (r.cells[col]?.textContent || '').trim();
        const number = s => /^[₦$£€\s\d,.()-]+$/.test(s) ? Number(s.replace(/[₦$£€\s,]/g, '').replace(/^\((.*)\)$/, '-$1')) : NaN;
        rows.sort((a,b) => { const x=value(a), y=value(b), nx=number(x), ny=number(y); return (Number.isFinite(nx)&&Number.isFinite(ny) ? nx-ny : x.localeCompare(y,undefined,{numeric:true}))*(asc?1:-1); });
        rows.forEach(r => body.append(r));
      };
    });
  });
  document.querySelectorAll('#pageContent form').forEach(form => {
    form.addEventListener('invalid', e => {
      const input=e.target; input.setAttribute('aria-invalid','true');
      if (!input.id) input.id=`field-${Math.random().toString(36).slice(2)}`;
      const id=input.id+'-error'; let error=document.getElementById(id);
      if (!error) { error=document.createElement('div'); error.id=id; error.className='field-error'; input.after(error); }
      error.textContent=input.validationMessage;
      input.setAttribute('aria-describedby', [...new Set((input.getAttribute('aria-describedby')||'').split(' ').filter(Boolean).concat(id))].join(' '));
    }, true);
    form.addEventListener('input', e => { if (e.target.validity?.valid) { e.target.removeAttribute('aria-invalid'); const error=document.getElementById(e.target.id+'-error'); if(error) error.textContent=''; } });
  });
})();
