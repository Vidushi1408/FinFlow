(function () {
    const root = document.documentElement;
    const STORAGE_KEY = 'finflow-theme';

    function applyTheme(theme) {
        root.setAttribute('data-bs-theme', theme);
        try { localStorage.setItem(STORAGE_KEY, theme); } catch (e) { /* private browsing, etc. */ }
    }

    let saved = null;
    try { saved = localStorage.getItem(STORAGE_KEY); } catch (e) { /* ignore */ }
    if (saved) applyTheme(saved);

    document.addEventListener('DOMContentLoaded', function () {
        const toggle = document.getElementById('themeToggle');
        if (!toggle) return;
        toggle.addEventListener('click', function () {
            const current = root.getAttribute('data-bs-theme') === 'light' ? 'dark' : 'light';
            applyTheme(current);
        });
    });
})();

// Shared helper: format a number the Indian way (1,00,000.00) for INR display.
function formatINR(amount) {
    const n = Number(amount) || 0;
    return '₹' + n.toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

// Shared helper: escape untrusted text (merchant names from uploaded CSVs, goal
// names, etc.) before it goes into an innerHTML template string.
function escapeHtml(value) {
    return String(value ?? '').replace(/[&<>"']/g, ch => ({
        '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
    }[ch]));
}

// Shared helper: CSRF token for fetch() POST/PATCH calls (Flask-WTF issues one per session).
function csrfToken() {
    const meta = document.querySelector('meta[name="csrf-token"]');
    return meta ? meta.getAttribute('content') : '';
}

// API errors are {"error": {"code", "message"}}; this reads the message out
// (with a plain-string fallback, in case a caller passes an older shape).
function errorMessage(data, fallback) {
    if (!data || !data.error) return fallback || 'Something went wrong.';
    return typeof data.error === 'string' ? data.error : (data.error.message || fallback);
}

async function apiFetch(url, options = {}) {
    const headers = Object.assign({}, options.headers, { 'X-CSRFToken': csrfToken() });
    if (options.body && !(options.body instanceof FormData)) {
        headers['Content-Type'] = 'application/json';
    }
    return fetch(url, Object.assign({}, options, { headers }));
}


// Shared helper: a confirmation the user has to actively type to pass, for
// destructive actions. Resolves true only if they typed `phrase` exactly.
function confirmTyped({ title, message, phrase, confirmLabel }) {
    return new Promise(resolve => {
        const wrapper = document.createElement('div');
        wrapper.className = 'modal fade';
        wrapper.tabIndex = -1;
        wrapper.innerHTML = `
            <div class="modal-dialog modal-dialog-centered">
                <div class="modal-content">
                    <div class="modal-header"><h5 class="modal-title">${escapeHtml(title)}</h5></div>
                    <div class="modal-body">
                        <p>${escapeHtml(message)}</p>
                        <label class="form-label">Type <strong>${escapeHtml(phrase)}</strong> to confirm</label>
                        <input type="text" class="form-control" autocomplete="off" spellcheck="false">
                    </div>
                    <div class="modal-footer">
                        <button type="button" class="btn btn-outline-secondary" data-cancel>Cancel</button>
                        <button type="button" class="btn btn-danger" data-confirm disabled>${escapeHtml(confirmLabel || 'Confirm')}</button>
                    </div>
                </div>
            </div>`;
        document.body.appendChild(wrapper);

        const modal = new bootstrap.Modal(wrapper);
        const input = wrapper.querySelector('input');
        const confirmBtn = wrapper.querySelector('[data-confirm]');
        let confirmed = false;

        input.addEventListener('input', () => { confirmBtn.disabled = input.value.trim() !== phrase; });
        input.addEventListener('keydown', e => { if (e.key === 'Enter' && !confirmBtn.disabled) confirmBtn.click(); });
        confirmBtn.addEventListener('click', () => { confirmed = true; modal.hide(); });
        wrapper.querySelector('[data-cancel]').addEventListener('click', () => modal.hide());
        wrapper.addEventListener('shown.bs.modal', () => input.focus());
        wrapper.addEventListener('hidden.bs.modal', () => { wrapper.remove(); resolve(confirmed); });
        modal.show();
    });
}
