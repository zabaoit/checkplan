/* gpt_signup_hybrid - Change Password tab logic */
(() => {
  'use strict';

  const LS_INPUT = 'gpt_reg.input.change_password';
  const MODE_TO_CONCURRENCY = {
    single: 1,
    multi: 2,
    multi3: 3,
    multi5: 5,
    multi10: 10,
    multi20: 20,
    multi30: 30,
    multi50: 50,
    multi100: 100,
    multi200: 200,
  };

  const state = {
    jobs: new Map(),
    order: [],
    activeJobId: null,
  };
  const detailCache = new Map();

  const $ = (id) => document.getElementById(id);
  const dom = {
    comboInput: $('chpwd-combo-input'),
    newPassword: $('chpwd-new-password'),
    mode: $('chpwd-mode'),
    jobTimeout: $('chpwd-job-timeout'),
    proxyToggle: $('chpwd-proxy-toggle'),
    btnRun: $('chpwd-btn-run'),
    btnStopAll: $('chpwd-btn-stop-all'),
    btnClearInput: $('chpwd-btn-clear-input'),
    comboCount: $('chpwd-combo-count'),
    jobList: $('chpwd-job-list'),
    jobSummary: $('chpwd-job-summary'),
    logPane: $('chpwd-log-pane'),
    logTarget: $('chpwd-log-target'),
    successPane: $('chpwd-success-pane'),
    errorPane: $('chpwd-error-pane'),
    btnCopySuccess: $('chpwd-btn-copy-success'),
    btnCopyMomo: $('chpwd-btn-copy-momo'),
    btnExportCsv: $('chpwd-btn-export-csv'),
    btnCopyError: $('chpwd-btn-copy-error'),
    btnClearDone: $('chpwd-btn-clear-done'),
    momoModal: $('momo-qr-modal'),
    momoModalClose: $('momo-qr-modal-close'),
    momoModalOk: $('momo-qr-modal-ok'),
    momoModalImg: $('momo-qr-modal-img'),
    momoModalEmail: $('momo-qr-modal-email'),
    momoModalPlan: $('momo-qr-modal-plan'),
    momoModalPaylink: $('momo-qr-modal-paylink'),
    momoModalCopyLink: $('momo-qr-modal-copy-link'),
    momoModalOpenBtn: $('momo-qr-modal-open-btn'),
    momoModalLoading: $('momo-qr-modal-loading'),
  };

  if (!dom.comboInput) return;

  function escHtml(value) {
    return String(value).replace(/[&<>"']/g, (char) => ({
      '&': '&amp;',
      '<': '&lt;',
      '>': '&gt;',
      '"': '&quot;',
      "'": '&#39;',
    }[char]));
  }

  function fmtDuration(secs) {
    if (secs == null) return '';
    if (secs < 60) return secs.toFixed(1) + 's';
    return Math.floor(secs / 60) + 'm' + Math.floor(secs % 60) + 's';
  }

  function api(path, opts = {}) {
    const token = window.GptUi.getAuthToken();
    const headers = {
      'Content-Type': 'application/json',
      ...(token ? { 'X-API-Token': token } : {}),
      ...(opts.headers || {}),
    };
    return fetch(path, { ...opts, headers }).then((resp) => {
      if (!resp.ok) {
        return resp.text().then((text) => {
          throw new Error(`HTTP ${resp.status}: ${text}`);
        });
      }
      return resp.json();
    });
  }

  function updateComboCount() {
    const lines = dom.comboInput.value.split('\n').filter((line) => {
      const trimmed = line.trim();
      return trimmed && !trimmed.startsWith('#');
    });
    dom.comboCount.textContent = `${lines.length} combo${lines.length === 1 ? '' : 's'}`;
  }

  function planBadge(job) {
    const isError = job.status === 'error';
    const textToCheck = isError ? String(job.error || '') : String(job.plan || '');

    if (isError) {
      if (textToCheck.toUpperCase().includes('KHÔNG CÓ OFFER')) {
        const amountMatch = textToCheck.match(/\[(.*?)\]/);
        const amountStr = amountMatch ? amountMatch[1] : '';

        const methodsMatch = textToCheck.match(/\((.*?)\)/);
        const methodsStr = methodsMatch ? methodsMatch[1] : '';
        const methods = methodsStr ? methodsStr.split(',').map(s => s.trim()) : [];

        let html = `<span class="plan-badge plan-err">KHÔNG CÓ OFFER</span>`;
        if (amountStr) {
          html += `<span class="plan-badge plan-price-tag">${escHtml(amountStr)}</span>`;
        }
        if (methods.length > 0 || amountStr) {
          html += `<span class="payment-tag payment-tag-stripe">Stripe</span>`;
        }
        for (const m of methods) {
          if (!m) continue;
          const mUpper = m.toUpperCase();
          if (mUpper === 'APPLE PAY' || mUpper === 'APPLEPAY') {
            html += `<span class="payment-tag payment-tag-applepay">Apple Pay</span>`;
          } else if (mUpper === 'GPAY' || mUpper === 'GOOGLE PAY' || mUpper === 'GOOGLEPAY') {
            html += `<span class="payment-tag payment-tag-gpay">Google Pay</span>`;
          } else if (mUpper === 'MOMO') {
            html += `<span class="payment-tag payment-tag-momo">MoMo</span>`;
            html += `<button class="btn-momo-qr" data-action="momo-qr" data-id="${escHtml(job.id)}" title="Quét mã QR MoMo">🌸 QR</button>`;
          }
        }
        return html;
      }
      return `<span class="plan-badge plan-err">${escHtml(textToCheck.slice(0, 30) || 'KHÔNG CÓ OFFER')}</span>`;
    }

    let planStr = job.plan ? String(job.plan) : '';
    if (!planStr) return '';

    if (planStr.toUpperCase().includes('CÓ OFFER')) {
      const match = planStr.match(/CÓ OFFER\s*(?:\[(.*?)\])?\s*(?:\((.*?)\))?/i);
      const customTag = match && match[1] ? match[1] : '';
      let methodsStr = match && match[2] ? match[2] : '';
      if (!methodsStr) {
        const altMatch = planStr.match(/\((.*?)\)/);
        if (altMatch) methodsStr = altMatch[1];
      }
      const methods = methodsStr ? methodsStr.split(',').map(s => s.trim()) : [];
      
      let html = '';
      if (customTag) {
        html += `<span class="plan-badge plan-offer-discount">${escHtml(customTag)}</span>`;
      } else {
        html += `<span class="plan-badge plan-offer-yes">Trial · 0 đ</span>`;
      }
      html += `<span class="payment-tag payment-tag-stripe">Stripe</span>`;
      
      for (const m of methods) {
        if (!m) continue;
        const mUpper = m.toUpperCase();
        if (mUpper === 'APPLE PAY' || mUpper === 'APPLEPAY') {
          html += `<span class="payment-tag payment-tag-applepay">Apple Pay</span>`;
        } else if (mUpper === 'GPAY' || mUpper === 'GOOGLE PAY' || mUpper === 'GOOGLEPAY') {
          html += `<span class="payment-tag payment-tag-gpay">Google Pay</span>`;
        } else if (mUpper === 'MOMO') {
          html += `<span class="payment-tag payment-tag-momo">MoMo</span>`;
          html += `<button class="btn-momo-qr" data-action="momo-qr" data-id="${escHtml(job.id)}" title="Quét mã QR MoMo">🌸 QR</button>`;
        }
      }
      return html;
    }

    const badgeClass = `plan-${escHtml(planStr.toLowerCase())}`;
    return `<span class="plan-badge ${badgeClass}">${escHtml(planStr.toUpperCase())}</span>`;
  }

  function renderJobs() {
    if (state.order.length === 0) {
      dom.jobList.innerHTML = '<div class="empty">Paste accounts and click Run.</div>';
      dom.jobSummary.textContent = '0 total';
      return;
    }
    const stats = { queued: 0, running: 0, success: 0, error: 0, cancelled: 0 };
    const html = state.order.map((id, index) => {
      const job = state.jobs.get(id);
      if (!job) return '';
      stats[job.status] = (stats[job.status] || 0) + 1;
      const cls = state.activeJobId === id ? 'job is-active' : 'job';
      let actionButtons = '';
      if (job.status === 'running') {
        actionButtons = `<button class="icon-btn icon-danger" data-action="remove" data-id="${escHtml(id)}" title="Stop">${window.GptUi.icon('stop')}</button>`;
      } else if (job.status === 'success') {
        actionButtons = `
          <button class="icon-btn" data-action="copy-json" data-id="${escHtml(id)}" title="Copy session JSON">${window.GptUi.icon('copy')}</button>
          <button class="icon-btn" data-action="copy-token" data-id="${escHtml(id)}" title="Copy access token">${window.GptUi.icon('token')}</button>
          <button class="icon-btn" data-action="retry" data-id="${escHtml(id)}" title="Retry">${window.GptUi.icon('retry')}</button>
        `;
      } else {
        actionButtons = `<button class="icon-btn" data-action="retry" data-id="${escHtml(id)}" title="Retry">${window.GptUi.icon('retry')}</button>`;
      }
      const badges = planBadge(job);
      const badgesRow = badges ? `<div class="job-badges-row">${badges}</div>` : '';
      return `
        <div class="${cls}" data-id="${escHtml(id)}">
          <div class="job-index">${index + 1}</div>
          <div class="job-status status-${escHtml(job.status)}">${escHtml(job.status)}</div>
          <div class="job-main">
            <div class="job-email" title="${escHtml(job.email)}">
              <span class="job-email-text">${escHtml(job.email)}</span>
            </div>
            ${badgesRow}
          </div>
          <div class="job-duration">${escHtml(fmtDuration(job.duration))}</div>
          <div class="job-actions">
            ${actionButtons}
            <button class="icon-btn icon-danger" data-action="remove" data-id="${escHtml(id)}" title="Remove">${window.GptUi.icon('remove')}</button>
          </div>
        </div>
      `;
    }).join('');
    dom.jobList.innerHTML = html;
    dom.jobSummary.textContent = [
      `${state.order.length} total`,
      stats.running ? `${stats.running} running` : '',
      stats.success ? `${stats.success} done` : '',
      stats.error ? `${stats.error} failed` : '',
    ].filter(Boolean).join(' | ');
  }

  function detailFor(jobId) {
    if (detailCache.has(jobId)) return Promise.resolve(detailCache.get(jobId));
    return api(`/api/change-password/jobs/${jobId}`).then((data) => {
      detailCache.set(jobId, data);
      return data;
    });
  }

  function renderOutputs() {
    const successLines = [];
    const errorLines = [];
    const pendingLoads = [];
    for (const id of state.order) {
      const job = state.jobs.get(id);
      if (!job) continue;
      if (job.status === 'success') {
        const detail = detailCache.get(id);
        if (!detail) {
          pendingLoads.push(detailFor(id).then(() => renderOutputs()).catch(() => {}));
          continue;
        }
        if (job.mode === 'change_2fa' || (!detail.new_password && detail.secret)) {
          const pw = detail.current_password || job.current_password || '';
          successLines.push(`${job.email}|${pw}|${detail.secret || ''}`);
        } else if (detail.new_password) {
          successLines.push(`${job.email}|${detail.new_password}|${detail.secret || ''}`);
        } else {
          successLines.push(`${job.email} -> ${job.plan || 'PLUS'}`);
        }
      } else if (job.status === 'error') {
        errorLines.push(`${job.email} -> ${job.error || 'ERROR'}`);
      }
    }
    dom.successPane.textContent = successLines.length
      ? successLines.join('\n')
      : 'SUCCESS OUTPUT:\nemail -> CÓ OFFER (GPay, PayPal...)';
    dom.errorPane.textContent = errorLines.length ? errorLines.join('\n') : 'ERROR OUTPUT:\nemail -> KHÔNG CÓ OFFER';
    void pendingLoads;
  }

  function renderLog(jobId) {
    if (!jobId) {
      dom.logPane.textContent = '';
      dom.logTarget.textContent = '-';
      return;
    }
    const job = state.jobs.get(jobId);
    if (!job) return;
    dom.logTarget.textContent = job.email;
    api(`/api/change-password/jobs/${jobId}/log`).then((data) => {
      dom.logPane.innerHTML = (data.log || []).map((line) => {
        const cls = /(error|fatal|failed|timeout)/i.test(line) ? 'log-line-error' : 'log-line-info';
        return `<span class="${cls}">${escHtml(line)}\n</span>`;
      }).join('');
      dom.logPane.scrollTop = dom.logPane.scrollHeight;
    }).catch((err) => {
      dom.logPane.textContent = `[error] ${err.message}`;
    });
  }

  function highlightInputLine(jobId) {
    const j = state.jobs.get(jobId);
    if (!j || !j.email) return;
    const text = dom.comboInput.value;
    if (!text) return;
    const lines = text.split('\n');
    const target = j.email.trim().toLowerCase();
    let offset = 0;
    let foundIndex = -1;
    let start = 0;
    let end = 0;
    for (let i = 0; i < lines.length; i++) {
      const line = lines[i];
      const email = line.trim().split('|')[0].trim().toLowerCase();
      if (email === target) {
        foundIndex = i;
        start = offset;
        end = offset + line.length;
        break;
      }
      offset += line.length + 1; // +1 cho ký tự '\n'
    }
    if (foundIndex === -1) return;
    dom.comboInput.focus();
    dom.comboInput.setSelectionRange(start, end);
    const lineHeight = 20;
    dom.comboInput.scrollTop = Math.max(0, (foundIndex - 2) * lineHeight);
  }

  function applySnapshot(snapshot) {
    state.order = (snapshot.jobs || []).map((job) => job.id);
    state.jobs.clear();
    for (const job of snapshot.jobs || []) state.jobs.set(job.id, job);
    for (const id of Array.from(detailCache.keys())) {
      if (!state.jobs.has(id)) detailCache.delete(id);
    }
    if (snapshot.job_timeout) dom.jobTimeout.value = snapshot.job_timeout;
    if (typeof snapshot.use_proxy === 'boolean') dom.proxyToggle.checked = snapshot.use_proxy;
    renderJobs();
    renderOutputs();
  }

  function applyJobUpdate(job) {
    const prev = state.jobs.get(job.id);
    state.jobs.set(job.id, job);
    if (!state.order.includes(job.id)) {
      state.order.push(job.id);
    }
    if (prev && prev.status !== job.status) detailCache.delete(job.id);
    renderJobs();
    renderOutputs();
    if (state.activeJobId === job.id) renderLog(job.id);
    if (job.status === 'success' && (!prev || prev.status !== 'success') && window.GptUi.playSuccessAlert) {
      window.GptUi.playSuccessAlert();
    }
  }

  function applyRemove(jobId) {
    state.jobs.delete(jobId);
    state.order = state.order.filter((id) => id !== jobId);
    detailCache.delete(jobId);
    if (state.activeJobId === jobId) {
      state.activeJobId = null;
      renderLog(null);
    }
    renderJobs();
    renderOutputs();
  }

  function applyLog(jobId, line) {
    if (state.activeJobId !== jobId) return;
    const cls = /(error|fatal|failed|timeout)/i.test(line) ? 'log-line-error' : 'log-line-info';
    const span = document.createElement('span');
    span.className = cls;
    span.textContent = line + '\n';
    dom.logPane.appendChild(span);
    dom.logPane.scrollTop = dom.logPane.scrollHeight;
  }

  async function syncConfig() {
    const selected = document.getElementById('mode')?.value || 'single';
    const maxConcurrent = Math.min(MODE_TO_CONCURRENCY[selected] || 1, 10);
    const timeout = parseInt(dom.jobTimeout.value, 10) || 240;
    return api('/api/change-password/config', {
      method: 'POST',
      body: JSON.stringify({
        max_concurrent: maxConcurrent,
        job_timeout: timeout,
        use_proxy: dom.proxyToggle.checked,
      }),
    });
  }

  dom.comboInput.addEventListener('input', () => {
    updateComboCount();
    window.GptUi.persistTextarea(LS_INPUT, dom.comboInput.value);
  });

  dom.btnRun.addEventListener('click', async () => {
    const combos = dom.comboInput.value.trim();
    if (!combos) {
      await Dialog.alert({ message: 'Paste accounts first.' });
      return;
    }
    const mode = dom.mode.value;
    const body = {
      combos,
      new_password: dom.newPassword.value.trim() || null,
      check_plan_only: mode === 'check_plan',
      skip_password_change: mode === 'session_only',
      change_2fa_only: mode === 'change_2fa',
    };
    if (mode === 'change_password' && !body.new_password) {
      const ok = await Dialog.confirm({
        message: 'New password is blank. The app will generate a random password for each account.',
        confirmLabel: 'Continue',
        cancelLabel: 'Cancel',
      });
      if (!ok) return;
    }
    dom.btnRun.disabled = true;
    try {
      await syncConfig();
      await api('/api/change-password/jobs', {
        method: 'POST',
        body: JSON.stringify(body),
      });
    } catch (err) {
      await Dialog.alert({ message: 'Error: ' + err.message });
    } finally {
      dom.btnRun.disabled = false;
    }
  });

  dom.jobTimeout.addEventListener('change', async () => {
    try {
      await syncConfig();
    } catch (err) {
      console.error(err);
    }
  });

  dom.proxyToggle.addEventListener('change', async () => {
    try {
      await syncConfig();
    } catch (err) {
      dom.proxyToggle.checked = !dom.proxyToggle.checked;
      await Dialog.alert({ message: err.message });
    }
  });

  dom.btnClearInput.addEventListener('click', () => {
    dom.comboInput.value = '';
    updateComboCount();
    window.GptUi.clearPersistedTextarea(LS_INPUT);
  });

  dom.btnStopAll.addEventListener('click', async () => {
    try {
      await api('/api/change-password/jobs/stop-all', { method: 'POST' });
    } catch (err) {
      await Dialog.alert({ message: err.message });
    }
  });

  dom.btnClearDone.addEventListener('click', async () => {
    try {
      await api('/api/change-password/jobs/clear-finished', { method: 'POST' });
    } catch (err) {
      await Dialog.alert({ message: err.message });
    }
  });

  dom.btnCopySuccess.addEventListener('click', () => {
    window.GptUi.copyText(dom.successPane.textContent);
  });

  dom.btnExportCsv.addEventListener('click', async () => {
    const successJobIds = [];
    for (const id of state.order) {
      const job = state.jobs.get(id);
      if (job && job.status === 'success') {
        successJobIds.push(id);
      }
    }
    if (successJobIds.length === 0) {
      await Dialog.alert({ message: 'No successful jobs to export.' });
      return;
    }
    dom.btnExportCsv.disabled = true;
    try {
      const promises = successJobIds.map(id => detailFor(id));
      const details = await Promise.all(promises);
      const csvRows = [];
      csvRows.push(['Email', 'Current Password', 'New Password', '2FA Secret', 'Plan'].map(v => `"${v.replace(/"/g, '""')}"`).join(','));
      for (const d of details) {
        const email = d.email || '';
        const currentPassword = d.current_password || '';
        const newPassword = d.new_password || '';
        const secret = d.secret || '';
        const plan = d.plan || 'unknown';
        const row = [email, currentPassword, newPassword, secret, plan]
          .map(v => `"${String(v).replace(/"/g, '""')}"`)
          .join(',');
        csvRows.push(row);
      }
      const csvContent = csvRows.join('\n');
      const blob = new Blob([new Uint8Array([0xEF, 0xBB, 0xBF]), csvContent], { type: 'text/csv;charset=utf-8;' });
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `change_password_success_${new Date().toISOString().slice(0,10)}.csv`;
      a.click();
      URL.revokeObjectURL(url);
    } catch (err) {
      await Dialog.alert({ message: 'Export failed: ' + err.message });
    } finally {
      dom.btnExportCsv.disabled = false;
    }
  });

  dom.btnCopyError.addEventListener('click', () => {
    window.GptUi.copyText(dom.errorPane.textContent);
  });

  dom.jobList.addEventListener('click', (event) => {
    const actionBtn = event.target.closest('[data-action]');
    if (actionBtn) {
      const action = actionBtn.dataset.action;
      const id = actionBtn.dataset.id;
      event.stopPropagation();
      if (action === 'retry') {
        api(`/api/change-password/jobs/${id}/retry`, { method: 'POST' }).catch(async (err) => {
          await Dialog.alert({ message: err.message });
        });
      } else if (action === 'remove') {
        api(`/api/change-password/jobs/${id}`, { method: 'DELETE' }).catch(async (err) => {
          await Dialog.alert({ message: err.message });
        });
      } else if (action === 'momo-qr') {
        openMomoModal(id);
        return;
      } else if (action === 'copy-json' || action === 'copy-token') {
        detailFor(id).then((detail) => {
          if (action === 'copy-json') {
            window.GptUi.copyText(JSON.stringify(detail.session_data || {}, null, 2));
          } else if (detail.session_data && detail.session_data.accessToken) {
            window.GptUi.copyText(detail.session_data.accessToken);
          }
        }).catch(async (err) => {
          await Dialog.alert({ message: err.message });
        });
      }
      return;
    }
    const row = event.target.closest('.job');
    if (row) {
      state.activeJobId = row.dataset.id;
      renderJobs();
      renderLog(state.activeJobId);
      highlightInputLine(state.activeJobId);
    }
  });

  let activeMomoUrl = '';

  function openMomoModal(jobId) {
    const job = state.jobs.get(jobId);
    if (!job) return;

    if (dom.momoModalEmail) dom.momoModalEmail.textContent = job.email || '-';
    if (dom.momoModalPlan) dom.momoModalPlan.textContent = job.plan || 'Trial · 0 đ (MoMo)';

    let initialUrl = job.momo_url || job.checkout_url || '#';
    activeMomoUrl = initialUrl;

    if (dom.momoModalPaylink) {
      dom.momoModalPaylink.href = initialUrl;
      dom.momoModalPaylink.textContent = initialUrl !== '#' ? initialUrl : '-';
    }
    if (dom.momoModalOpenBtn) dom.momoModalOpenBtn.href = initialUrl;

    if (dom.momoModalImg) {
      dom.momoModalImg.removeAttribute('src');
    }

    if (dom.momoModal) dom.momoModal.style.display = 'flex';
    if (dom.momoModalLoading) dom.momoModalLoading.style.display = 'block';

    api(`/api/change-password/jobs/${jobId}/momo-qr`, { method: 'POST' })
      .then((res) => {
        const finalUrl = res.momo_url || res.checkout_url || activeMomoUrl;
        const qrData = res.qr_data || finalUrl;
        activeMomoUrl = finalUrl;
        if (dom.momoModalPaylink) {
          dom.momoModalPaylink.href = finalUrl;
          dom.momoModalPaylink.textContent = finalUrl;
        }
        if (dom.momoModalOpenBtn) dom.momoModalOpenBtn.href = finalUrl;
        if (dom.momoModalImg && qrData && qrData !== '#') {
          dom.momoModalImg.src = `https://api.qrserver.com/v1/create-qr-code/?size=250x250&margin=8&data=${encodeURIComponent(qrData)}`;
        }
      })
      .catch((err) => {
        console.warn('MoMo direct confirm note:', err);
        if (dom.momoModalImg && activeMomoUrl && activeMomoUrl !== '#') {
          dom.momoModalImg.src = `https://api.qrserver.com/v1/create-qr-code/?size=250x250&margin=8&data=${encodeURIComponent(activeMomoUrl)}`;
        }
      })
      .finally(() => {
        if (dom.momoModalLoading) dom.momoModalLoading.style.display = 'none';
      });
  }

  dom.momoModalClose?.addEventListener('click', () => {
    if (dom.momoModal) dom.momoModal.style.display = 'none';
  });

  dom.momoModalOk?.addEventListener('click', () => {
    if (dom.momoModal) dom.momoModal.style.display = 'none';
  });

  dom.momoModalCopyLink?.addEventListener('click', async () => {
    if (activeMomoUrl && activeMomoUrl !== '#') {
      window.GptUi.copyText(activeMomoUrl);
      await Dialog.alert({ message: 'Đã copy link thanh toán MoMo!' });
    }
  });

  dom.btnCopyMomo?.addEventListener('click', async () => {
    const momoLinks = [];
    for (const id of state.order) {
      const job = state.jobs.get(id);
      if (!job) continue;
      const planText = String(job.plan || '') + String(job.error || '');
      const hasMomo = job.has_momo || planText.toUpperCase().includes('MOMO');
      if (hasMomo) {
        const url = job.momo_url || job.checkout_url || (job.offer_details && job.offer_details.checkout_url) || (job.offer_details && `https://chatgpt.com/checkout/openai_llc/${job.offer_details.session_id}`);
        if (url) {
          momoLinks.push(`${job.email}|${url}`);
        }
      }
    }
    if (momoLinks.length === 0) {
      await Dialog.alert({ message: 'Không tìm thấy tài khoản nào có MoMo.' });
      return;
    }
    window.GptUi.copyText(momoLinks.join('\n'));
    await Dialog.alert({ message: `Đã copy ${momoLinks.length} link MoMo vào clipboard!` });
  });

  SseBus.on('change_password', (data) => {
    if (data.type === 'snapshot') {
      applySnapshot(data);
    } else if (data.type === 'job') {
      applyJobUpdate(data.job);
    } else if (data.type === 'remove') {
      applyRemove(data.job_id);
    } else if (data.type === 'clear_finished') {
      api('/api/change-password/jobs').then((snapshot) => applySnapshot(snapshot)).catch(console.error);
    } else if (data.type === 'log') {
      applyLog(data.job_id, data.line);
    }
  });

  const savedInput = localStorage.getItem(LS_INPUT);
  if (savedInput) dom.comboInput.value = savedInput;
  updateComboCount();
  api('/api/change-password/config').then((cfg) => {
    if (cfg.job_timeout) dom.jobTimeout.value = cfg.job_timeout;
    if (typeof cfg.use_proxy === 'boolean') dom.proxyToggle.checked = cfg.use_proxy;
  }).catch(() => {});

  setInterval(() => {
    let hasRunning = false;
    for (const [, job] of state.jobs) {
      if (job.status === 'running' && job.started_at) {
        hasRunning = true;
        job.duration = (Date.now() / 1000) - job.started_at;
      }
    }
    if (hasRunning) renderJobs();
  }, 1000);
})();
