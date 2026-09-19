// Shared "upload a bank CSV" flow: preview how the file will be read (column
// mapping, sample rows, auto-assigned categories), let the user fix the
// mapping, and only then import. Nothing is saved until "Import" is clicked.
//
//   startUploadFlow(file, containerEl, { onImported(data), onCancel() })

const UPLOAD_FIELDS = [
    { key: 'date', label: 'Date', required: true },
    { key: 'description', label: 'Description', required: true },
    { key: 'amount', label: 'Amount', hint: 'single amount column' },
    { key: 'type', label: 'Type (Dr/Cr)', hint: 'optional' },
    { key: 'debit', label: 'Debit / Withdrawal', hint: 'if debits and credits are separate' },
    { key: 'credit', label: 'Credit / Deposit', hint: 'if debits and credits are separate' },
    { key: 'balance', label: 'Balance (optional)', hint: 'running balance, used to work out your real balance' },
];

function startUploadFlow(file, container, callbacks = {}) {
    let mapping = null; // null = let the server suggest; set once the user edits a dropdown
    let openingBalance = ''; // only sent when the statement has no balance column and the user typed one

    async function requestPreview() {
        const formData = new FormData();
        formData.append('file', file);
        if (mapping) formData.append('mapping', JSON.stringify(mapping));
        if (openingBalance) formData.append('opening_balance', openingBalance);
        const res = await apiFetch('/api/v1/transactions/upload/preview', { method: 'POST', body: formData });
        const data = await res.json();
        if (!res.ok) throw new Error(errorMessage(data, 'Could not read this file.'));
        return data;
    }

    async function refresh() {
        container.innerHTML = '<div class="text-secondary py-2">Reading your file&hellip;</div>';
        try {
            render(await requestPreview());
        } catch (err) {
            container.innerHTML = `<div class="alert alert-danger">${escapeHtml(err.message)}</div>`;
        }
    }

    function collectMapping() {
        const result = {};
        container.querySelectorAll('select[data-field]').forEach(sel => { result[sel.dataset.field] = sel.value || null; });
        return result;
    }

    function render(data) {
        mapping = data.mapping;

        const selects = UPLOAD_FIELDS.map(f => {
            const options = ['<option value="">&mdash; none &mdash;</option>'].concat(
                data.columns.map(c => `<option value="${escapeHtml(c)}" ${data.mapping[f.key] === c ? 'selected' : ''}>${escapeHtml(c)}</option>`)
            ).join('');
            return `<div class="col-md-4 col-6">
                <label class="form-label mb-1"><small>${f.label}${f.required ? ' *' : ''}</small></label>
                <select class="form-select form-select-sm" data-field="${f.key}">${options}</select>
            </div>`;
        }).join('');

        let body = '';
        if (data.error) {
            body += `<div class="alert alert-warning py-2">${escapeHtml(data.error)}</div>`;
        }
        (data.issues || []).forEach(i => {
            body += `<div class="alert alert-${i.level === 'warning' ? 'warning' : 'info'} py-2 mb-2">${escapeHtml(i.message)}</div>`;
        });

        if (data.ready) {
            const s = data.summary;
            body += `<p class="mb-2"><strong>${s.rows}</strong> transactions from ${escapeHtml(s.date_from)} to ${escapeHtml(s.date_to)} &middot;
                ${formatINR(s.total_debit)} spent (${s.debit_count}) &middot; ${formatINR(s.total_credit)} received (${s.credit_count})</p>`;

            if (s.already_imported > 0) {
                body += `<div class="alert alert-info py-2 mb-2">${s.already_imported} of these ${s.already_imported === 1 ? 'is' : 'are'} already imported and will be skipped.
                    ${s.new_rows > 0 ? s.new_rows + ' new will be added to your existing imported account.' : 'There is nothing new to add.'}</div>`;
            } else if (data.account && !data.account.is_new) {
                body += `<div class="alert alert-info py-2 mb-2">These will be added to your existing imported account.</div>`;
            }

            const ob = data.opening_balance;
            if (ob && ob.source === 'statement') {
                body += `<p class="mb-2"><small style="color: var(--text-secondary);">Opening balance ${formatINR(ob.value)} worked out from the statement's balance column.</small></p>`;
            } else if (ob && ob.editable) {
                body += `<div class="mb-3"><label class="form-label mb-1"><small>Balance before the first transaction in this file (optional)</small></label>
                    <input type="text" inputmode="decimal" class="form-control form-control-sm" style="max-width: 220px;" id="uploadOpeningBalance"
                        placeholder="e.g. 25000" value="${escapeHtml(openingBalance)}">
                    <small style="color: var(--text-secondary);">No balance column found. Leave blank to start from \u20B90 &mdash; your balance will then show the net change since this statement, not your real balance.</small></div>`;
            }

            const chips = Object.entries(data.category_counts)
                .map(([cat, n]) => `<span class="badge text-bg-secondary me-1">${escapeHtml(cat)} ${n}</span>`).join('');
            body += `<p class="mb-2"><small style="color: var(--text-secondary);">Auto-categorized:</small> ${chips}</p>`;

            const rows = data.preview.map(p => `<tr>
                <td>${escapeHtml(p.date)}</td><td>${escapeHtml(p.description)}</td>
                <td>${escapeHtml(p.category_id)}</td><td>${escapeHtml(p.transaction_type)}</td>
                <td class="text-end">${formatINR(p.amount)}</td></tr>`).join('');
            body += `<div class="table-responsive"><table class="table table-sm">
                <thead><tr><th>Date</th><th>Description</th><th>Category</th><th>Type</th><th class="text-end">Amount</th></tr></thead>
                <tbody>${rows}</tbody></table></div>
                <small style="color: var(--text-secondary);">Showing the first ${data.preview.length} of ${s.rows}. You can change any category after importing.</small>`;
        }

        container.innerHTML = `
            <div class="card">
                <h3>Check how we read ${escapeHtml(file.name)}</h3>
                <p style="color: var(--text-secondary);">We matched your columns automatically. If anything looks wrong, pick the right column below.</p>
                <div class="row g-2 mb-3">${selects}</div>
                ${body}
                <div id="uploadFlowMsg"></div>
                <div class="d-flex gap-2 mt-3">
                    <button class="btn btn-primary" id="uploadFlowImport" ${data.ready && data.summary.new_rows > 0 ? '' : 'disabled'}>
                        ${data.ready ? (data.summary.new_rows > 0 ? 'Import ' + data.summary.new_rows + ' new transactions' : 'Nothing new to import') : 'Import'}
                    </button>
                    <button class="btn btn-outline-secondary" id="uploadFlowCancel">Cancel</button>
                </div>
            </div>`;

        container.querySelectorAll('select[data-field]').forEach(sel => {
            sel.addEventListener('change', () => { mapping = collectMapping(); refresh(); });
        });
        const openingInput = container.querySelector('#uploadOpeningBalance');
        if (openingInput) {
            openingInput.addEventListener('change', () => { openingBalance = openingInput.value.trim(); });
        }
        container.querySelector('#uploadFlowCancel').addEventListener('click', () => {
            container.innerHTML = '';
            if (callbacks.onCancel) callbacks.onCancel();
        });
        container.querySelector('#uploadFlowImport').addEventListener('click', importNow);
    }

    async function importNow() {
        const btn = container.querySelector('#uploadFlowImport');
        const msg = container.querySelector('#uploadFlowMsg');
        btn.disabled = true;
        btn.textContent = 'Importing…';

        const formData = new FormData();
        formData.append('file', file);
        formData.append('mapping', JSON.stringify(mapping));
        const typed = container.querySelector('#uploadOpeningBalance');
        if (typed && typed.value.trim()) formData.append('opening_balance', typed.value.trim());
        const res = await apiFetch('/api/v1/transactions/upload', { method: 'POST', body: formData });
        const data = await res.json();

        if (!res.ok) {
            msg.innerHTML = `<div class="alert alert-danger py-2 mt-2">${escapeHtml(errorMessage(data, 'Import failed.'))}</div>`;
            btn.disabled = false;
            btn.textContent = 'Try again';
            return;
        }
        const skipped = data.duplicates_skipped > 0 ? ` (${data.duplicates_skipped} already imported, skipped)` : '';
        const flagged = data.fraud_alerts_generated > 0 ? `, ${data.fraud_alerts_generated} flagged as unusual` : '';
        container.innerHTML = `<div class="alert alert-success">Imported ${data.transactions_added} new transaction${data.transactions_added === 1 ? '' : 's'}${skipped}${flagged}.</div>`;
        if (callbacks.onImported) callbacks.onImported(data);
    }

    refresh();
}
