(() => {
  const form=document.getElementById('voucherForm');
  if (!form) return;
  const get=id=>document.getElementById(id);
  const kind=get('kind'), purpose=get('purpose'), lines=get('lines'), dialog=get('reviewDialog');
  const currency=cents=>'₦'+(cents/100).toLocaleString('en-NG',{minimumFractionDigits:2,maximumFractionDigits:2});
  const cents=v=>Math.round((Number(v)||0)*100);
  let valid=false, posting=false;
  function section(id, visible) {
    const el=get(id); el.hidden=!visible;
    el.querySelectorAll('input,select,button').forEach(input=>input.disabled=!visible);
  }
  function reindex() {
    const rows=[...lines.querySelectorAll('.voucher-line')];
    rows.forEach((row,i)=>{
      row.querySelector('legend').textContent='Line '+(i+1);
      row.querySelectorAll('[data-field]').forEach(input=>{
        const old=input.id; input.id=input.name=input.dataset.field+'_'+i;
        const label=[...row.querySelectorAll('label')].find(l=>l.htmlFor===old);
        if(label) label.htmlFor=input.id;
      });
      row.querySelector('.remove-line').disabled=rows.length===1;
    });
    get('lineCount').value=rows.length;
  }
  function refresh() {
    const journal=kind.value==='journal', payment=kind.value==='payment';
    if(!payment) purpose.value='general';
    const disbursement=payment&&purpose.value==='loan_disbursement';
    section('moneySection',!journal); section('purposeGroup',payment); section('loanGroup',disbursement);
    section('linesSection',!disbursement);
    get('party').required=!journal; get('bank_account').required=!journal;
    get('disbursement_loan_id').required=disbursement;
    get('partyLabel').textContent=payment?'Paid to *':'Received from *';
    get('bankLabel').textContent=payment?'Paid from *':'Received into *';
    get('sumBank').textContent=journal?'Selected on journal lines':get('bank_account').selectedOptions[0]?.textContent||'Choose a bank / cash account';
    let debit=0,credit=0,total=0;
    valid=true;
    lines.querySelectorAll('.voucher-line').forEach(row=>{
      row.querySelectorAll('.cash-line,.journal-line').forEach(group=>{
        const visible=group.classList.contains('cash-line')?!journal:journal;
        group.hidden=!visible; group.querySelectorAll('input').forEach(input=>input.disabled=!visible||disbursement);
      });
      const d=cents(row.querySelector('[data-field=debit]').value), c=cents(row.querySelector('[data-field=credit]').value);
      const a=cents(row.querySelector('[data-field=amount]').value);
      debit+=d; credit+=c; total+=a;
      if(journal ? (d>0)===(c>0)||d<0||c<0 : a<=0) valid=false;
    });
    const totals=get('totals'); totals.replaceChildren();
    function pair(label, value) { const p=document.createElement('p'); p.className='d-flex justify-content-between gap-2'; const text=document.createElement('span'); text.textContent=label; const strong=document.createElement('strong'); strong.textContent=currency(value); p.append(text,strong); totals.append(p); }
    if(disbursement) {
      const gross=cents(get('disbursement_loan_id').selectedOptions[0]?.dataset.amount);
      const fee=Math.round(gross*.01), insurance=Math.round(gross*.01);
      pair('Gross loan',gross); pair('Application fee (1%)',fee); pair('Insurance (1%)',insurance); pair('Net bank payment',gross-fee-insurance);
      valid=gross>0;
    } else if(journal) { pair('Total debits',debit); pair('Total credits',credit); pair('Difference',Math.abs(debit-credit)); valid=valid&&debit===credit&&debit>0; }
    else { pair(payment?'Bank credit / payment':'Bank debit / receipt',total); }
    get('reviewButton').disabled=!valid;
    get('reviewButton').title=valid?'Review before posting':'Enter positive amounts; journal debits and credits must balance';
    reindex();
  }
  get('addLine').onclick=()=>{
    if(lines.children.length>=100) return;
    const row=lines.querySelector('.voucher-line').cloneNode(true);
    row.querySelectorAll('input,select').forEach(input=>{input.value='';input.removeAttribute('aria-invalid');input.removeAttribute('aria-describedby');});
    row.querySelectorAll('.field-error').forEach(el=>el.remove());
    lines.append(row); reindex(); refresh(); row.querySelector('select').focus();
  };
  lines.addEventListener('click',e=>{if(e.target.classList.contains('remove-line')&&lines.children.length>1){e.target.closest('.voucher-line').remove();refresh();}});
  form.addEventListener('input',refresh); form.addEventListener('change',refresh);
  form.addEventListener('submit',e=>{
    if(posting) return;
    e.preventDefault(); refresh();
    if(!valid||!form.reportValidity()) return;
    const review=get('reviewDetails'); review.replaceChildren();
    ['kind','date','party','bank_account','description','reference','disbursement_loan_id'].forEach(id=>{
      const input=get(id); if(input.disabled||!input.value) return;
      const p=document.createElement('p');
      const label=document.querySelector('label[for="'+id+'"]')?.textContent||id;
      p.textContent=label.replace(' *','')+': '+(input.tagName==='SELECT'?input.selectedOptions[0].textContent:input.value);
      review.append(p);
    });
    if(!get('linesSection').hidden) lines.querySelectorAll('.voucher-line').forEach(row=>{
      const p=document.createElement('p');
      p.textContent=[...row.querySelectorAll('input,select')].filter(i=>!i.disabled&&i.value).map(i=>i.tagName==='SELECT'?i.selectedOptions[0].textContent:i.value).join(' · ');
      review.append(p);
    });
    review.append(get('totals').cloneNode(true)); review.lastChild.removeAttribute('id');
    dialog.showModal();
  });
  get('editVoucher').onclick=()=>dialog.close();
  get('confirmVoucher').onclick=()=>{ if(!valid||!form.reportValidity()) return; posting=true; get('confirmVoucher').disabled=true; get('confirmVoucher').textContent='Posting…'; form.requestSubmit(); };
  refresh();
})();
