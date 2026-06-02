function openModal() {
                document.getElementById("statsModal").style.display = "block";
            }
            function closeModal() {
                document.getElementById("statsModal").style.display = "none";
            }

            function _apiMethodTag(method) {
                const m = String(method || 'GET').toUpperCase().replace(/&/g, '&amp;').replace(/</g, '&lt;');
                const cls = m === 'POST' ? 'api-method-tag--post'
                    : m === 'GET' ? 'api-method-tag--get'
                    : m === 'PUT' ? 'api-method-tag--put'
                    : m === 'DELETE' ? 'api-method-tag--delete'
                    : 'api-method-tag--other';
                return `<span class="api-method-tag ${cls}">${m}</span>`;
            }

            function _renderApiStatsList(stats) {
                const body = document.getElementById('apiStatsModalBody');
                if (!body) return;
                if (!stats || !stats.length) {
                    body.innerHTML = '<div class="api-stats-empty">No requests tracked yet</div>';
                    return;
                }
                let total = 0;
                let html = '';
                stats.forEach(stat => {
                    const count = stat.request_count || 0;
                    total += count;
                    const ep = String(stat.endpoint || '').replace(/&/g, '&amp;').replace(/</g, '&lt;');
                    html += `<div class="api-stat-item">
                        <div class="api-stat-line">
                            ${_apiMethodTag(stat.method)}
                            <span class="api-stat-sep" aria-hidden="true">|</span>
                            <span class="api-endpoint">${ep}</span>
                        </div>
                        <div class="api-count">${count}</div>
                    </div>`;
                });
                html += `<div class="api-stat-item api-stat-item--total">
                    <div class="api-stat-line">
                        <span class="api-method-tag api-method-tag--total">ALL</span>
                        <span class="api-stat-sep" aria-hidden="true">|</span>
                        <span class="api-endpoint">All endpoints</span>
                    </div>
                    <div class="api-count api-count--total">${total}</div>
                </div>`;
                body.innerHTML = html;
            }

            function openApiStatsModal() {
                const modal = document.getElementById('apiStatsModal');
                const body = document.getElementById('apiStatsModalBody');
                if (!modal || !body) return;
                modal.style.display = 'block';
                body.innerHTML = '<div class="api-stats-empty">Loading…</div>';
                fetch('/api_stats')
                    .then(r => r.ok ? r.json() : Promise.reject(new Error('HTTP ' + r.status)))
                    .then(data => _renderApiStatsList(data.api_stats || []))
                    .catch(() => {
                        body.innerHTML = '<div class="api-stats-empty">Failed to load API statistics.</div>';
                    });
            }

            function closeApiStatsModal() {
                const modal = document.getElementById('apiStatsModal');
                if (modal) modal.style.display = 'none';
            }

            // ── Main nav: Jobs vs Workers ─────────────────────────────────────
            let _workerSubtab = 'active';
            const TABLE_PAGE_SIZE_OPTIONS = [10, 20, 50, 100, 200, 500, 1000];
            let _workerPage = 1;
            let _workerPageSize = 50;
            let _workerFiltersData = null;
            let _workerPageRows = [];
            let _workerListTotal = 0;
            let _workerSearchQuery = '';
            let _workerSearchDebounce = null;
            const _workerSelectedIds = new Set();
            let _workerActionPreviewState = null;
            let _workerDetailCache = null;
            let _workerHistoryPage = 0;
            let _workerHistoryTotal = 0;
            let _workerHistoryLoading = false;
            let _workerMetricsHistoryIndex = 0;
            let _workerMetricsHistoryTotal = 0;
            let _workerMetricsHistoryEntry = null;
            let _workerMetricsHistoryLoading = false;
            let _workerMetricsFetchGen = 0;
            let _workerMetricsPlayTimer = null;
            const WORKER_HISTORY_PAGE_SIZE = 10;
            const WORKER_METRICS_PLAY_INTERVAL_MS = 500;

            function switchMainView(view) {
                const jobsView = document.getElementById('view-jobs');
                const workersView = document.getElementById('view-workers');
                const tabJobs = document.getElementById('mainTabJobs');
                const tabWorkers = document.getElementById('mainTabWorkers');
                if (!jobsView || !workersView) return;
                if (view === 'workers') {
                    jobsView.classList.remove('active');
                    workersView.classList.add('active');
                    tabJobs.classList.remove('active');
                    tabWorkers.classList.add('active');
                    tabJobs.setAttribute('aria-selected', 'false');
                    tabWorkers.setAttribute('aria-selected', 'true');
                    refreshWorkersPage();
                } else {
                    workersView.classList.remove('active');
                    jobsView.classList.add('active');
                    tabWorkers.classList.remove('active');
                    tabJobs.classList.add('active');
                    tabWorkers.setAttribute('aria-selected', 'false');
                    tabJobs.setAttribute('aria-selected', 'true');
                }
            }

            function switchWorkerSubtab(which) {
                _workerSubtab = which;
                _workerPage = 1;
                clearWorkerSelection(false);
                _updateWorkersTableTitle();
                document.getElementById('workerSubtabActive').classList.toggle('active', which === 'active');
                document.getElementById('workerSubtabPending').classList.toggle('active', which === 'pending');
                const pausedBtn = document.getElementById('workerSubtabPaused');
                if (pausedBtn) pausedBtn.classList.toggle('active', which === 'paused');
                document.getElementById('workerSubtabDisabled').classList.toggle('active', which === 'disabled');
                const activeToolbar = document.getElementById('workersActiveToolbar');
                const pendingToolbar = document.getElementById('workersPendingToolbar');
                const pausedToolbar = document.getElementById('workersPausedToolbar');
                if (activeToolbar) activeToolbar.style.display = which === 'active' ? 'flex' : 'none';
                if (pendingToolbar) pendingToolbar.style.display = which === 'pending' ? 'flex' : 'none';
                if (pausedToolbar) pausedToolbar.style.display = which === 'paused' ? 'flex' : 'none';
                loadWorkerSummary();
                loadWorkerFilters().then(() => loadWorkersPageTable());
            }

            function _workerListLifecycle() {
                if (_workerSubtab === 'disabled') return 'disabled';
                if (_workerSubtab === 'pending') return 'pending';
                if (_workerSubtab === 'paused') return 'paused';
                return 'active';
            }

            function loadWorkerSummary() {
                fetch('/workers/summary')
                    .then(r => r.ok ? r.json() : null)
                    .then(data => {
                        if (!data) return;
                        const map = {
                            workerIdleCount: data.idle,
                            workerBusyCount: data.busy,
                            workerPendingCount: data.pending_commands,
                            workersPageIdle: data.idle,
                            workersPageBusy: data.busy,
                            workersPageDisabled: data.disabled,
                            workerTabPendingCount: data.pending_commands,
                            workerTabPausedCount: data.paused,
                        };
                        Object.keys(map).forEach(id => {
                            const el = document.getElementById(id);
                            if (el) el.textContent = map[id] ?? 0;
                        });
                    })
                    .catch(() => {});
            }

            function _escHtml(s) {
                return String(s ?? '')
                    .replace(/&/g, '&amp;')
                    .replace(/</g, '&lt;')
                    .replace(/>/g, '&gt;')
                    .replace(/"/g, '&quot;');
            }

            function _disabledCheckboxTd(className) {
                return `<td class="${className}"><input type="checkbox" disabled aria-label="Selection unavailable"></td>`;
            }

            function _parseWorkerIdParts(workerId) {
                const raw = String(workerId || '').trim();
                const parts = raw.split('_');
                if (parts.length >= 3 && /^\d+$/.test(parts[parts.length - 1])) {
                    return {
                        host: parts.slice(0, -2).join('_') || parts[0],
                        instance: parts[parts.length - 2],
                        slot: parseInt(parts[parts.length - 1], 10),
                    };
                }
                return { host: raw || '—', instance: '—', slot: '—' };
            }

            function _workerRowIdentity(w) {
                let workerId = String(w.worker_id || '').trim();
                const hostRaw = String(w.host || '').trim();
                if (!workerId && hostRaw.split('_').length >= 3) {
                    workerId = hostRaw;
                }
                const parsed = workerId ? _parseWorkerIdParts(workerId) : null;
                const host = (hostRaw && hostRaw !== workerId) ? hostRaw : (parsed?.host || '—');
                const instance = String(w.instance || '').trim() || (parsed?.instance ?? '—');
                const slot = (w.slot != null && w.slot !== '') ? w.slot : (parsed?.slot ?? '—');
                return { workerId: workerId || '—', host, instance, slot };
            }

            function _workersTableTitle() {
                return {
                    active: 'Active Workers',
                    pending: 'Pending Workers',
                    paused: 'Paused Workers',
                    disabled: 'Stopped Workers',
                }[_workerSubtab] || 'Workers';
            }

            function _updateWorkersTableTitle() {
                const el = document.getElementById('workersTableTitle');
                if (el) el.textContent = _workersTableTitle();
            }

            function _workersTableOverlayHtml(kind, message) {
                if (kind === 'loading') {
                    return '<div class="loading-message"><i class="fas fa-spinner fa-spin"></i> Loading workers…</div>';
                }
                const isError = kind === 'error';
                const title = isError ? 'Error Loading Workers' : 'No workers found';
                const sub = message || (isError ? 'Please try again.' : 'No workers on this tab.');
                return `<div class="empty-state${isError ? ' error-state' : ''}">
                    <div class="empty-icon"><i class="fas fa-${isError ? 'exclamation-triangle' : 'server'}"></i></div>
                    <div class="empty-text">${title}</div>
                    <div class="empty-subtext">${isError ? _escHtml(sub) : sub}</div>
                </div>`;
            }

            function _setWorkersTableOverlay(html, visible) {
                const overlay = document.getElementById('workers-table-overlay');
                const tbody = document.getElementById('workersPageBody');
                if (!overlay) return;
                overlay.innerHTML = html;
                overlay.style.display = visible ? 'flex' : 'none';
                if (visible && tbody) tbody.innerHTML = '';
            }

            function _workerDesiredState(w) {
                return (w.desired_state || 'run').toLowerCase();
            }

            function _isWorkerPaused(w) {
                return _workerDesiredState(w) === 'pause';
            }

            function _workerControlTerminal(w) {
                const ds = _workerDesiredState(w);
                return ds === 'drain' || ds === 'stop';
            }

            function _workersTabSupportsSelection() {
                return _workerSubtab !== 'disabled';
            }

            function _workerFilterContext() {
                return {
                    lifecycle: _workerListLifecycle(),
                    host: document.getElementById('workerFilterHost')?.value || null,
                    instance: document.getElementById('workerFilterInstance')?.value || null,
                    slot: document.getElementById('workerFilterSlot')?.value ?? null,
                    q: _workerSearchQuery || null,
                };
            }

            function _workerActionLabels() {
                return {
                    run: 'Resume',
                    pause: 'Pause',
                    drain: 'Drain',
                    stop: 'Stop',
                    cancel: 'Cancel pending',
                };
            }

            function clearWorkerSelection(refreshBar) {
                _workerSelectedIds.clear();
                if (refreshBar !== false) {
                    _updateWorkerBulkBar();
                    _syncWorkerPageCheckboxes();
                }
            }

            function toggleWorkerSelection(workerId, checked) {
                if (!workerId) return;
                if (checked) {
                    _workerSelectedIds.add(workerId);
                } else {
                    _workerSelectedIds.delete(workerId);
                }
                _updateWorkerBulkBar();
                _syncWorkerPageCheckboxes();
            }

            function toggleWorkerSelectAllPage(checked) {
                if (!_workersTabSupportsSelection()) return;
                _workerPageRows.forEach(w => {
                    const wid = _workerRowIdentity(w).workerId;
                    if (!wid || wid === '—') return;
                    if (checked) {
                        _workerSelectedIds.add(wid);
                    } else {
                        _workerSelectedIds.delete(wid);
                    }
                });
                _updateWorkerBulkBar();
                _syncWorkerPageCheckboxes();
            }

            function _syncWorkerPageCheckboxes() {
                const checkAll = document.getElementById('workersCheckAllPage');
                const thCheck = document.getElementById('workersCheckAllTh');
                const selectable = _workersTabSupportsSelection();
                if (thCheck) thCheck.style.display = '';
                if (!checkAll) return;
                checkAll.disabled = !selectable;
                const pageIds = _workerPageRows.map(w => {
                    const id = _workerRowIdentity(w).workerId;
                    return id !== '—' ? id : '';
                }).filter(Boolean);
                if (!selectable) {
                    checkAll.checked = false;
                    checkAll.indeterminate = false;
                } else {
                    const selectedOnPage = pageIds.filter(id => _workerSelectedIds.has(id)).length;
                    checkAll.checked = pageIds.length > 0 && selectedOnPage === pageIds.length;
                    checkAll.indeterminate = selectedOnPage > 0 && selectedOnPage < pageIds.length;
                }
                document.querySelectorAll('.workers-row-check').forEach(cb => {
                    const wid = cb.dataset.workerId;
                    if (wid) cb.checked = _workerSelectedIds.has(wid);
                });
            }

            function _updateWorkerBulkBar() {
                const bar = document.getElementById('workersBulkBar');
                const countEl = document.getElementById('workersBulkCount');
                const chipsEl = document.getElementById('workersBulkChips');
                const actionsEl = document.getElementById('workersBulkActions');
                const selected = Array.from(_workerSelectedIds).sort();
                const n = selected.length;
                if (!bar || !countEl || !actionsEl) return;
                if (!n || !_workersTabSupportsSelection()) {
                    bar.style.display = 'none';
                    if (chipsEl) chipsEl.innerHTML = '';
                    return;
                }
                bar.style.display = 'flex';

                const pageIds = new Set(_workerPageRows.map(w => {
                    const id = _workerRowIdentity(w).workerId;
                    return id !== '—' ? id : '';
                }).filter(Boolean));
                const offPage = selected.filter(id => !pageIds.has(id)).length;
                countEl.textContent = offPage > 0
                    ? `${n} selected · ${offPage} not on this page`
                    : `${n} selected`;

                if (chipsEl) {
                    const maxChips = 12;
                    const visible = selected.slice(0, maxChips);
                    const overflow = selected.length - visible.length;
                    chipsEl.innerHTML =
                        visible.map(id =>
                            `<button type="button" class="jobs-selection-chip" title="Remove ${_escHtml(id)}" onclick="toggleWorkerSelection(decodeURIComponent('${encodeURIComponent(id)}'), false)">${_escHtml(id)}<span class="jobs-selection-chip-x">&times;</span></button>`,
                        ).join('') +
                        (overflow > 0
                            ? `<span class="jobs-selection-chip jobs-selection-chip--more">+${overflow} more</span>`
                            : '');
                }

                let html = '';
                if (_workerSubtab === 'active') {
                    html += `<button type="button" class="workers-btn-sm workers-btn--pause" onclick="requestWorkerAction('pause','workers')"><i class="fas fa-pause"></i> Pause selected</button>`;
                    html += `<button type="button" class="workers-btn-sm workers-btn--drain" onclick="requestWorkerAction('drain','workers')">Drain selected</button>`;
                    html += `<button type="button" class="workers-btn-sm workers-btn--stop" onclick="requestWorkerAction('stop','workers')">Stop selected</button>`;
                } else if (_workerSubtab === 'paused') {
                    html += `<button type="button" class="workers-btn-sm workers-btn--run" onclick="requestWorkerAction('run','workers')"><i class="fas fa-play"></i> Resume selected</button>`;
                } else if (_workerSubtab === 'pending') {
                    html += `<button type="button" class="workers-btn-sm workers-btn--cancel" onclick="requestWorkerAction('cancel','workers')">Cancel selected</button>`;
                }
                actionsEl.innerHTML = html;
            }

            function onWorkerSearchInput() {
                const input = document.getElementById('workerSearchInput');
                if (!input) return;
                _syncSearchClearBtn(input);
                clearTimeout(_workerSearchDebounce);
                _workerSearchDebounce = setTimeout(() => {
                    _workerSearchQuery = (input.value || '').trim();
                    _workerPage = 1;
                    loadWorkersPageTable();
                }, 280);
            }

            function searchWorkers() {
                const input = document.getElementById('workerSearchInput');
                _workerSearchQuery = (input?.value || '').trim();
                _workerPage = 1;
                loadWorkersPageTable();
            }

            function clearWorkerSearch() {
                const input = document.getElementById('workerSearchInput');
                if (input) input.value = '';
                _syncSearchClearBtn(input);
                _workerSearchQuery = '';
                _workerPage = 1;
                loadWorkersPageTable();
            }

            function _syncSearchClearBtn(inputEl) {
                if (!inputEl) return;
                const clearBtn = inputEl.closest('.search-input-group')?.querySelector('.clear-btn');
                if (!clearBtn) return;
                clearBtn.classList.toggle('is-visible', (inputEl.value || '').length > 0);
            }

            function closeAllSearchHelp() {
                document.querySelectorAll('.search-help-popover').forEach(p => { p.hidden = true; });
                document.querySelectorAll('.search-info-btn').forEach(b => {
                    b.setAttribute('aria-expanded', 'false');
                    b.classList.remove('is-active');
                });
            }

            function toggleSearchHelp(btn, ev) {
                if (ev) ev.stopPropagation();
                const wrap = btn.closest('.search-wrap');
                const popover = wrap?.querySelector('.search-help-popover');
                if (!popover) return;
                const willOpen = popover.hidden;
                closeAllSearchHelp();
                if (willOpen) {
                    popover.hidden = false;
                    btn.setAttribute('aria-expanded', 'true');
                    btn.classList.add('is-active');
                }
            }

            document.addEventListener('click', (ev) => {
                if (ev.target.closest('.search-info-btn') || ev.target.closest('.search-help-popover')) return;
                closeAllSearchHelp();
            });

            document.addEventListener('keydown', (ev) => {
                if (ev.key === 'Escape') closeAllSearchHelp();
            });

            function _updateWorkersSelectAllMatchingBtn() {
                const btn = document.getElementById('workersSelectAllMatchingBtn');
                if (!btn) return;
                const search = _workerSearchQuery.trim();
                const total = _workerListTotal || 0;
                if (!search || !total || !_workersTabSupportsSelection()) {
                    btn.style.display = 'none';
                    return;
                }
                btn.style.display = 'inline';
                btn.textContent = `Add all ${total} matching to selection`;
                btn.disabled = false;
            }

            async function selectAllMatchingWorkers() {
                if (!_workersTabSupportsSelection() || !_workerSearchQuery.trim()) return;
                const btn = document.getElementById('workersSelectAllMatchingBtn');
                if (btn) btn.disabled = true;
                const search = _workerSearchQuery.trim();
                try {
                    let page = 1;
                    let totalPages = 1;
                    let added = 0;
                    while (page <= totalPages) {
                        const params = _workerFilterParams();
                        params.set('page', String(page));
                        params.set('per_page', String(_workerPageSize));
                        params.set('q', search);
                        const r = await fetch('/workers/list?' + params.toString());
                        const data = await r.json();
                        if (!r.ok) throw new Error(data.error || 'Request failed');
                        totalPages = data.total_pages || 1;
                        (data.workers || []).forEach(w => {
                            const wid = _workerRowIdentity(w).workerId;
                            if (wid && wid !== '—') _workerSelectedIds.add(wid);
                        });
                        added += (data.workers || []).length;
                        page += 1;
                    }
                    showNotification(`Added ${added} worker(s) to selection.`, 'info');
                    _updateWorkerBulkBar();
                    _syncWorkerPageCheckboxes();
                } catch (e) {
                    showNotification('Failed to select workers: ' + e.message, 'error');
                } finally {
                    if (btn) btn.disabled = false;
                }
            }

            function _buildWorkerRowActions(w) {
                const identity = _workerRowIdentity(w);
                const wid = identity.workerId === '—' ? '' : identity.workerId;
                const widEsc = _escHtml(wid);
                const widJs = encodeURIComponent(wid);
                let actions = `<button type="button" class="workers-btn-sm" onclick="openWorkerDetail('${widEsc}')">Details</button>`;
                if (_workerSubtab === 'pending') {
                    actions += `<button type="button" class="workers-btn-sm workers-btn--cancel" onclick="requestWorkerAction('cancel','worker',decodeURIComponent('${widJs}'))">Cancel</button>`;
                    return actions;
                }
                if (_workerSubtab === 'paused') {
                    actions += `<button type="button" class="workers-btn-sm workers-btn--run" onclick="requestWorkerAction('run','worker',decodeURIComponent('${widJs}'))"><i class="fas fa-play"></i> Resume</button>`;
                    return actions;
                }
                if (_workerSubtab !== 'active') {
                    return actions;
                }
                if (_workerControlTerminal(w)) {
                    if (w.pending) {
                        actions += `<button type="button" class="workers-btn-sm workers-btn--cancel" onclick="requestWorkerAction('cancel','worker',decodeURIComponent('${widJs}'))">Cancel</button>`;
                    }
                    return actions;
                }
                const paused = _isWorkerPaused(w);
                if (paused) {
                    actions += `<button type="button" class="workers-btn-sm workers-btn--run" onclick="requestWorkerAction('run','worker',decodeURIComponent('${widJs}'))"><i class="fas fa-play"></i> Resume</button>`;
                } else {
                    actions += `<button type="button" class="workers-btn-sm workers-btn--pause" onclick="requestWorkerAction('pause','worker',decodeURIComponent('${widJs}'))"><i class="fas fa-pause"></i> Pause</button>`;
                }
                const drainDisabled = paused ? ' disabled' : '';
                const drainClass = paused ? ' workers-btn-sm--disabled' : ' workers-btn--drain';
                actions += `<button type="button" class="workers-btn-sm${drainClass}"${drainDisabled} onclick="requestWorkerAction('drain','worker',decodeURIComponent('${widJs}'))">Drain</button>`;
                actions += `<button type="button" class="workers-btn-sm workers-btn--stop" onclick="requestWorkerAction('stop','worker',decodeURIComponent('${widJs}'))">Stop</button>`;
                if (w.pending) {
                    actions += `<button type="button" class="workers-btn-sm workers-btn--cancel" onclick="requestWorkerAction('cancel','worker',decodeURIComponent('${widJs}'))">Cancel</button>`;
                }
                return actions;
            }

            function _workerStateBadge(w) {
                if ((w.lifecycle_status || 'active') === 'disabled') {
                    return '<span class="worker-badge worker-badge--disabled">disabled</span>';
                }
                if (w.pending) {
                    return `<span class="worker-badge worker-badge--pending">queued ${w.desired_state}</span>`;
                }
                const ds = w.desired_state || 'run';
                if (ds !== 'run') {
                    const appliedCls = ds === 'pause' ? 'worker-badge--pause'
                        : ds === 'drain' ? 'worker-badge--drain'
                        : ds === 'stop' ? 'worker-badge--stop'
                        : 'worker-badge--applied';
                    const label = ds === 'pause' ? 'paused' : ds;
                    return `<span class="worker-badge ${appliedCls}">${label}</span>`;
                }
                const rs = w.reported_status || 'idle';
                const cls = rs === 'busy' ? 'worker-badge--busy' : 'worker-badge--idle';
                return `<span class="worker-badge ${cls}">${rs}</span>`;
            }

            function loadWorkerFilters() {
                return fetch('/workers/filters?lifecycle=' + _workerListLifecycle())
                    .then(r => r.json())
                    .then(data => {
                        _workerFiltersData = data;
                        const hostSel = document.getElementById('workerFilterHost');
                        const instSel = document.getElementById('workerFilterInstance');
                        const slotSel = document.getElementById('workerFilterSlot');
                        if (!hostSel) return;
                        const prevHost = hostSel.value;
                        const prevInst = instSel.value;
                        const prevSlot = slotSel.value;
                        hostSel.innerHTML = '<option value="">All hosts</option>';
                        (data.hosts || []).forEach(h => {
                            hostSel.innerHTML += `<option value="${_escHtml(h)}">${_escHtml(h)}</option>`;
                        });
                        hostSel.value = (data.hosts || []).includes(prevHost) ? prevHost : '';
                        _populateWorkerInstanceDropdown(true);
                        if ((data.instances_by_host[hostSel.value] || []).includes(prevInst)) {
                            instSel.value = prevInst;
                        }
                        _populateWorkerSlotDropdown(true);
                        if (prevSlot !== '' && slotSel.querySelector(`option[value="${prevSlot}"]`)) {
                            slotSel.value = prevSlot;
                        }
                    })
                    .catch(() => {});
            }

            function _populateWorkerInstanceDropdown(preserveSelection) {
                const hostSel = document.getElementById('workerFilterHost');
                const instSel = document.getElementById('workerFilterInstance');
                if (!instSel || !_workerFiltersData) return;
                const host = hostSel?.value || '';
                const prevInst = preserveSelection ? instSel.value : '';
                instSel.innerHTML = '<option value="">All instances</option>';
                if (host) {
                    (_workerFiltersData.instances_by_host[host] || []).forEach(inst => {
                        instSel.innerHTML += `<option value="${_escHtml(inst)}">${_escHtml(inst)}</option>`;
                    });
                    instSel.removeAttribute('disabled');
                } else {
                    instSel.setAttribute('disabled', 'disabled');
                }
                if (prevInst && instSel.querySelector(`option[value="${CSS.escape(prevInst)}"]`)) {
                    instSel.value = prevInst;
                }
            }

            function _populateWorkerSlotDropdown(preserveSelection) {
                const hostSel = document.getElementById('workerFilterHost');
                const instSel = document.getElementById('workerFilterInstance');
                const slotSel = document.getElementById('workerFilterSlot');
                if (!slotSel || !_workerFiltersData) return;
                const host = hostSel?.value || '';
                const inst = instSel?.value || '';
                const prevSlot = preserveSelection ? slotSel.value : '';
                slotSel.innerHTML = '<option value="">All slots</option>';
                if (host && inst) {
                    const key = host + '|' + inst;
                    (_workerFiltersData.slots_by_host_instance[key] || []).forEach(sl => {
                        slotSel.innerHTML += `<option value="${sl}">${sl}</option>`;
                    });
                    slotSel.removeAttribute('disabled');
                } else {
                    slotSel.setAttribute('disabled', 'disabled');
                }
                if (prevSlot !== '' && slotSel.querySelector(`option[value="${CSS.escape(prevSlot)}"]`)) {
                    slotSel.value = prevSlot;
                }
            }

            function onWorkerHostFilterChange() {
                _workerPage = 1;
                const instSel = document.getElementById('workerFilterInstance');
                const slotSel = document.getElementById('workerFilterSlot');
                if (instSel) instSel.value = '';
                if (slotSel) slotSel.value = '';
                _populateWorkerInstanceDropdown(false);
                _populateWorkerSlotDropdown(false);
                loadWorkersPageTable();
            }

            function onWorkerInstanceFilterChange() {
                _workerPage = 1;
                const slotSel = document.getElementById('workerFilterSlot');
                if (slotSel) slotSel.value = '';
                _populateWorkerSlotDropdown(false);
                loadWorkersPageTable();
            }

            function onWorkerSlotFilterChange() {
                _workerPage = 1;
                loadWorkersPageTable();
            }

            function onWorkerFilterChange() {
                onWorkerHostFilterChange();
            }

            function _workerFilterParams() {
                const p = new URLSearchParams();
                p.set('lifecycle', _workerListLifecycle());
                p.set('page', String(_workerPage));
                p.set('per_page', String(_workerPageSize));
                const host = document.getElementById('workerFilterHost')?.value;
                const inst = document.getElementById('workerFilterInstance')?.value;
                const slot = document.getElementById('workerFilterSlot')?.value;
                if (host) p.set('host', host);
                if (inst) p.set('instance', inst);
                if (slot !== '') p.set('slot', slot);
                if (_workerSearchQuery) p.set('q', _workerSearchQuery);
                return p;
            }

            function _updateWorkersPagination(data) {
                const total = data.total_count || 0;
                const page = data.current_page || 1;
                const totalPages = data.total_pages || 0;
                const perPage = data.per_page || _workerPageSize;
                const pageInfo = document.getElementById('workersPageInfo');
                const paginationInfo = document.getElementById('workersPaginationInfo');
                const prevBtn = document.getElementById('workersPagePrev');
                const nextBtn = document.getElementById('workersPageNext');
                if (pageInfo) {
                    pageInfo.textContent = totalPages
                        ? `Page ${page} of ${totalPages}`
                        : 'Page 1';
                }
                if (paginationInfo) {
                    if (!total) {
                        paginationInfo.textContent = 'No workers';
                    } else {
                        const start = ((page - 1) * perPage) + 1;
                        const end = Math.min(page * perPage, total);
                        paginationInfo.textContent =
                            `Showing ${start}-${end} of ${total} workers`;
                    }
                }
                if (prevBtn) prevBtn.disabled = page <= 1;
                if (nextBtn) nextBtn.disabled = !totalPages || page >= totalPages;
                _workerPage = page;
            }

            function changeWorkerPageSize(size) {
                const n = parseInt(size, 10);
                if (!TABLE_PAGE_SIZE_OPTIONS.includes(n)) return;
                _workerPageSize = n;
                const sel = document.getElementById('workerPageSize');
                if (sel) sel.value = String(n);
                _workerPage = 1;
                loadWorkersPageTable();
            }

            function changeWorkersPage(direction) {
                const next = _workerPage + direction;
                if (next >= 1) {
                    _workerPage = next;
                    loadWorkersPageTable();
                }
            }

            function _workersEmptyTableRow(message) {
                const check = _workersTabSupportsSelection()
                    ? '<td class="workers-td-check"></td>'
                    : _disabledCheckboxTd('workers-td-check');
                return `<tr class="table-empty-row">${check}<td colspan="9" class="table-empty-cell">${message}</td></tr>`;
            }

            function _workersErrorTableRow(message) {
                return `<tr class="table-empty-row table-error-row">${_disabledCheckboxTd('workers-td-check')}<td colspan="9" class="table-empty-cell table-error-cell">${_escHtml(message)}</td></tr>`;
            }

            function loadWorkersPageTable() {
                const tbody = document.getElementById('workersPageBody');
                if (!tbody) return;
                _setWorkersTableOverlay(_workersTableOverlayHtml('loading'), true);
                fetch('/workers/list?' + _workerFilterParams().toString())
                    .then(r => r.json())
                    .then(data => {
                        _updateWorkersPagination(data);
                        const workers = data.workers || [];
                        _workerPageRows = workers;
                        _workerListTotal = data.total_count || 0;
                        if (!workers.length) {
                            const msg = _workerSubtab === 'disabled'
                                ? (_workerSearchQuery
                                    ? 'No stopped workers match your search.'
                                    : 'No stopped workers.')
                                : _workerSubtab === 'pending'
                                    ? (_workerSearchQuery
                                        ? 'No pending workers match your search.'
                                        : 'No pending commands. Workers appear here after you queue pause, drain, or stop until their next poll (~3 min).')
                                    : _workerSubtab === 'paused'
                                        ? (_workerSearchQuery
                                            ? 'No paused workers match your search.'
                                            : 'No paused workers. Use Pause on an active worker; it moves here after the command is applied.')
                                        : (_workerSearchQuery
                                            ? 'No active workers match your search.'
                                            : 'No active workers. Start workers with <code>jd_worker_cli</code> — they appear after the first poll (~3 min).');
                            _setWorkersTableOverlay('', false);
                            tbody.innerHTML = _workersEmptyTableRow(msg);
                            _syncWorkerPageCheckboxes();
                            _updateWorkerBulkBar();
                            return;
                        }
                        _setWorkersTableOverlay('', false);
                        const selectable = _workersTabSupportsSelection();
                        tbody.innerHTML = workers.map(w => {
                            const identity = _workerRowIdentity(w);
                            const wid = identity.workerId === '—' ? '' : identity.workerId;
                            const widEsc = _escHtml(wid || identity.workerId);
                            const jobCell = w.current_job_id ? `#${w.current_job_id}` : '—';
                            const completedJobs = Number.isFinite(w.completed_jobs)
                                ? w.completed_jobs
                                : (parseInt(w.completed_jobs, 10) || 0);
                            const lastPoll = _formatWorkerPollTime(
                                w.last_poll_at ?? w.last_poll_at_fmt,
                            );
                            const actions = _buildWorkerRowActions(w);
                            const checked = wid && _workerSelectedIds.has(wid) ? ' checked' : '';
                            const widEnc = encodeURIComponent(wid);
                            const checkCell = selectable && wid
                                ? `<td class="workers-td-check"><input type="checkbox" class="workers-row-check" data-worker-id="${widEnc}"${checked} onchange="toggleWorkerSelection(decodeURIComponent(this.dataset.workerId), this.checked)" aria-label="Select ${_escHtml(wid)}"></td>`
                                : _disabledCheckboxTd('workers-td-check');
                            return `<tr>
                                ${checkCell}
                                <td><code class="workers-id">${widEsc}</code></td>
                                <td>${_escHtml(identity.host)}</td>
                                <td><code>${_escHtml(identity.instance)}</code></td>
                                <td>${identity.slot !== '—' ? identity.slot : '—'}</td>
                                <td>${_workerStateBadge(w)}</td>
                                <td>${jobCell}</td>
                                <td>${completedJobs}</td>
                                <td class="workers-td-poll">${_escHtml(lastPoll)}</td>
                                <td class="workers-row-actions">${actions}</td>
                            </tr>`;
                        }).join('');
                        _syncWorkerPageCheckboxes();
                        _updateWorkerBulkBar();
                    })
                    .catch(err => {
                        _workerPageRows = [];
                        _setWorkersTableOverlay('', false);
                        tbody.innerHTML = _workersErrorTableRow(err.message || 'Failed to load workers.');
                        _updateWorkerBulkBar();
                    })
                    .finally(() => {
                        _updateWorkersSelectAllMatchingBtn();
                    });
            }

            function refreshWorkersPage() {
                _updateWorkersTableTitle();
                loadWorkerSummary();
                loadWorkerFilters().then(() => loadWorkersPageTable());
            }

            function _workerActionScopeLabel(action, scope, target) {
                const labels = _workerActionLabels();
                const label = labels[action] || action;
                if (scope === 'workers') {
                    return `${label} ${_workerSelectedIds.size} selected worker(s)`;
                }
                if (scope === 'all') {
                    const tab = _workerSubtab;
                    const search = _workerSearchQuery ? ` matching "${_workerSearchQuery}"` : '';
                    const filters = [];
                    const host = document.getElementById('workerFilterHost')?.value;
                    const inst = document.getElementById('workerFilterInstance')?.value;
                    if (host) filters.push(`host ${host}`);
                    if (inst) filters.push(`instance ${inst}`);
                    const filt = filters.length ? ` (${filters.join(', ')})` : '';
                    return `${label} all on ${tab} tab${search}${filt}`;
                }
                if (scope === 'worker') {
                    return `${label} worker ${target}`;
                }
                return `${label} ${scope} ${target}`;
            }

            function _workerActionApplyNote(action) {
                if (action === 'stop') {
                    return 'Workers abort any current job, exit, and move to Stopped (~3 min if reachable).';
                }
                if (action === 'cancel') {
                    return 'Queued commands are reverted on the next poll (~3 min).';
                }
                return 'Applies on the next worker poll (~3 min).';
            }

            function _buildWorkerActionPayload(action, scope, target) {
                const ctx = _workerFilterContext();
                const payload = {
                    action,
                    scope,
                    lifecycle: ctx.lifecycle,
                    host: ctx.host,
                    instance: ctx.instance,
                    q: ctx.q,
                };
                if (ctx.slot !== null && ctx.slot !== '') {
                    payload.slot = ctx.slot;
                }
                if (scope === 'worker') {
                    payload.target = target;
                } else if (scope === 'workers') {
                    payload.worker_ids = Array.from(_workerSelectedIds);
                }
                return payload;
            }

            function _renderWorkerPreviewList(workers) {
                if (!workers.length) {
                    return '<p class="workers-action-preview-empty">No workers would be affected.</p>';
                }
                const rows = workers.map(w => {
                    const job = w.current_job_id ? `#${w.current_job_id}` : '—';
                    const st = w.pending
                        ? `pending ${w.desired_state || ''}`
                        : (w.desired_state && w.desired_state !== 'run'
                            ? w.desired_state
                            : (w.reported_status || 'idle'));
                    return `<tr>
                        <td><code>${_escHtml(w.worker_id)}</code></td>
                        <td>${_escHtml(w.host || '—')}</td>
                        <td>${_escHtml(w.instance || '—')}</td>
                        <td>${w.slot != null ? w.slot : '—'}</td>
                        <td>${_escHtml(st)}</td>
                        <td>${job}</td>
                    </tr>`;
                }).join('');
                return `<table class="workers-action-preview-table">
                    <thead><tr>
                        <th>Worker ID</th><th>Host</th><th>Instance</th><th>Slot</th><th>Status</th><th>Job</th>
                    </tr></thead>
                    <tbody>${rows}</tbody>
                </table>`;
            }

            function closeWorkerActionPreviewModal() {
                const modal = document.getElementById('workerActionPreviewModal');
                if (modal) modal.style.display = 'none';
                _workerActionPreviewState = null;
            }

            function confirmWorkerActionPreview() {
                const st = _workerActionPreviewState;
                if (!st) return;
                const btn = document.getElementById('workerActionPreviewConfirm');
                if (btn) btn.disabled = true;
                const url = st.action === 'cancel' ? '/workers/cancel' : '/workers/command';
                const body = { ...st.payload };
                if (st.action === 'cancel') {
                    delete body.action;
                }
                fetch(url, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(body),
                })
                    .then(r => r.json().then(d => ({ ok: r.ok, data: d })))
                    .then(({ ok, data }) => {
                        if (btn) btn.disabled = false;
                        closeWorkerActionPreviewModal();
                        if (!ok || !data.success) {
                            showNotification(data.error || 'Action failed.', 'error');
                            return;
                        }
                        const labels = _workerActionLabels();
                        const label = labels[st.action] || st.action;
                        if (st.action === 'cancel') {
                            showNotification(
                                `Reverted ${data.reverted} pending command(s).`,
                                'success',
                            );
                            clearWorkerSelection();
                            refreshWorkersPage();
                            return;
                        }
                        showNotification(
                            `Queued ${label.toLowerCase()} for ${data.affected} worker(s).`,
                            'success',
                        );
                        clearWorkerSelection();
                        if (data.affected > 0 && st.action === 'stop') {
                            refreshWorkersPage();
                        } else if (data.affected > 0 && st.action !== 'run') {
                            switchWorkerSubtab('pending');
                        } else {
                            refreshWorkersPage();
                        }
                    })
                    .catch(e => {
                        if (btn) btn.disabled = false;
                        showNotification('Network error: ' + e.message, 'error');
                    });
            }

            function requestWorkerAction(action, scope, target) {
                if (scope === 'workers' && _workerSelectedIds.size === 0) {
                    showNotification('Select at least one worker.', 'warning');
                    return;
                }
                const payload = _buildWorkerActionPayload(action, scope, target);
                const previewPayload = { ...payload };
                if (action === 'cancel') {
                    previewPayload.action = 'cancel';
                }
                fetch('/workers/preview', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(previewPayload),
                })
                    .then(r => r.json().then(d => ({ ok: r.ok, data: d })))
                    .then(({ ok, data }) => {
                        if (!ok || !data.success) {
                            showNotification(data.error || 'Preview failed.', 'error');
                            return;
                        }
                        const workers = data.workers || [];
                        const count = data.count ?? workers.length;
                        const modal = document.getElementById('workerActionPreviewModal');
                        const title = document.getElementById('workerActionPreviewTitle');
                        const summary = document.getElementById('workerActionPreviewSummary');
                        const list = document.getElementById('workerActionPreviewList');
                        const confirmBtn = document.getElementById('workerActionPreviewConfirm');
                        if (!modal || !title || !summary || !list) return;

                        const scopeLabel = _workerActionScopeLabel(action, scope, target);
                        title.textContent = count
                            ? `Confirm: ${scopeLabel}`
                            : 'No workers affected';
                        summary.textContent = count
                            ? `${count} worker(s) will be affected. ${_workerActionApplyNote(action)}`
                            : `No workers match this action on the current tab${_workerSearchQuery ? ` (search: "${_workerSearchQuery}")` : ''}.`;
                        list.innerHTML = _renderWorkerPreviewList(workers);

                        if (confirmBtn) {
                            const labels = _workerActionLabels();
                            confirmBtn.textContent = count
                                ? `Confirm ${labels[action] || action}`
                                : 'Close';
                            confirmBtn.className = 'workers-btn ' + (
                                action === 'stop' ? 'workers-btn--stop'
                                : action === 'cancel' ? 'workers-btn--cancel'
                                : action === 'run' ? 'workers-btn--run'
                                : action === 'pause' ? 'workers-btn--pause'
                                : action === 'drain' ? 'workers-btn--drain'
                                : 'workers-btn--pause'
                            );
                            confirmBtn.onclick = count
                                ? () => confirmWorkerActionPreview()
                                : () => closeWorkerActionPreviewModal();
                        }

                        _workerActionPreviewState = count
                            ? { action, payload, scope, target }
                            : null;
                        modal.style.display = 'block';
                    })
                    .catch(e => showNotification('Network error: ' + e.message, 'error'));
            }

            function workerCommand(action, scope, target) {
                requestWorkerAction(action, scope, target);
            }

            function cancelWorkerCommand(scope, target) {
                requestWorkerAction('cancel', scope, target);
            }

            function switchWorkerModalTab(tab, btn) {
                if (tab !== 'metrics') {
                    stopWorkerHistoryMetricsPlayback();
                }
                ['info', 'history', 'metrics'].forEach(t => {
                    document.getElementById('modalTab-worker-' + t).classList.toggle('active', t === tab);
                });
                document.querySelectorAll('#workerDetailModal .modal-tab-btn').forEach(b => b.classList.remove('active'));
                if (btn) btn.classList.add('active');
                if (tab === 'history') {
                    loadWorkerHistoryPage(_workerHistoryPage);
                }
                if (tab === 'metrics') {
                    loadWorkerMetricsView(false);
                }
            }

            function openWorkerDetail(workerId) {
                fetch('/workers/detail?worker_id=' + encodeURIComponent(workerId))
                    .then(r => r.json())
                    .then(w => {
                        if (w.error) {
                            showNotification(w.error, 'error');
                            return;
                        }
                        _workerDetailCache = w;
                        _workerHistoryPage = 0;
                        _workerHistoryTotal = w.history_total || 0;
                        _workerMetricsHistoryIndex = 0;
                        _workerMetricsHistoryTotal = 0;
                        _workerMetricsHistoryEntry = null;
                        stopWorkerHistoryMetricsPlayback();
                        document.getElementById('workerDetailId').textContent = w.worker_id;
                        const badge = document.getElementById('workerDetailBadge');
                        const lc = w.lifecycle_status || 'active';
                        badge.textContent = lc === 'disabled' ? 'DISABLED' : (w.reported_status || 'idle').toUpperCase();
                        badge.className = 'job-status-badge badge-' + (lc === 'disabled' ? 'DELETED' : 'SERVED');

                        const info = document.getElementById('workerInfoTable');
                        const rows = [
                            ['Host', w.host || '—'],
                            ['Instance', w.instance || '—'],
                            ['Slot', w.slot != null ? w.slot : '—'],
                            ['Machine type', w.machine_type || '—'],
                            ['Status', w.reported_status || '—'],
                            ['Desired state', w.desired_state || 'run'],
                            ['Current job', w.current_job_id ? '#' + w.current_job_id : '—'],
                            ['Completed jobs', String(
                                Number.isFinite(w.completed_jobs)
                                    ? w.completed_jobs
                                    : (parseInt(w.completed_jobs, 10) || 0),
                            )],
                            ['Worker version', w.jd_worker_version || '—'],
                            ['First poll', _formatWorkerPollTime(w.first_poll_at ?? w.first_poll_at_fmt)],
                            ['Last poll', _formatWorkerPollTime(w.last_poll_at ?? w.last_poll_at_fmt)],
                            ['Disabled at', _formatWorkerPollTime(w.disabled_at ?? w.disabled_at_fmt)],
                        ];
                        info.innerHTML = '<table class="modal-kv-table"><tr><th>Field</th><th>Value</th></tr>' +
                            rows.map(([k, v]) => `<tr><td><strong>${k}</strong></td><td>${_escHtml(String(v))}</td></tr>`).join('') +
                            '</table>';

                        const timeline = document.getElementById('workerHistoryTimeline');
                        timeline.innerHTML =
                            '<div class="timeline-empty"><i class="fas fa-history"></i>Open the History tab to load events.</div>';
                        const histPag = document.getElementById('workerHistoryPagination');
                        if (histPag) histPag.style.display = 'none';

                        const metricsPanel = document.getElementById('workerMetricsPanel');
                        const metricsControls = document.getElementById('workerHistoryMetricsControls');
                        const metricsLabel = document.getElementById('workerHistoryMetricsEventLabel');
                        const metricsTime = document.getElementById('workerMetricsSnapshotTime');
                        if (metricsPanel) {
                            metricsPanel.innerHTML =
                                '<p class="worker-metrics-empty">Loading metrics…</p>';
                        }
                        if (metricsControls) metricsControls.style.display = 'none';
                        if (metricsLabel) {
                            metricsLabel.style.display = 'none';
                            metricsLabel.textContent = '';
                        }
                        if (metricsTime) metricsTime.textContent = '—';

                        switchWorkerModalTab('info', document.getElementById('tab-btn-worker-info'));
                        document.getElementById('workerDetailModal').style.display = 'block';
                    })
                    .catch(e => showNotification('Could not load worker: ' + e.message, 'error'));
            }

            function closeWorkerDetailModal() {
                stopWorkerHistoryMetricsPlayback();
                document.getElementById('workerDetailModal').style.display = 'none';
            }

            function _workerHistoryQuery(page, pageSize, metricsOnly) {
                const wid = _workerDetailCache?.worker_id;
                if (!wid) return Promise.reject(new Error('No worker selected'));
                const p = new URLSearchParams();
                p.set('worker_id', wid);
                p.set('page', String(page));
                p.set('page_size', String(pageSize));
                if (metricsOnly) p.set('metrics_only', '1');
                return fetch('/workers/history?' + p.toString()).then(r => {
                    if (!r.ok) {
                        return r.json().then(d => {
                            throw new Error(d.error || r.statusText);
                        });
                    }
                    return r.json();
                });
            }

            function loadWorkerHistoryPage(page) {
                const timelineEl = document.getElementById('workerHistoryTimeline');
                const pagEl = document.getElementById('workerHistoryPagination');
                const infoEl = document.getElementById('workerHistoryPaginationInfo');
                const prevBtn = document.getElementById('workerHistoryPrev');
                const nextBtn = document.getElementById('workerHistoryNext');
                if (!timelineEl || !_workerDetailCache || _workerHistoryLoading) return;

                _workerHistoryLoading = true;
                timelineEl.innerHTML =
                    '<div class="timeline-empty"><i class="fas fa-spinner fa-spin"></i> Loading history…</div>';
                if (pagEl) pagEl.style.display = 'none';

                _workerHistoryQuery(page, WORKER_HISTORY_PAGE_SIZE, false)
                    .then(data => {
                        _workerHistoryPage = data.page ?? page;
                        _workerHistoryTotal = data.total ?? 0;
                        const entries = data.entries || [];
                        timelineEl.innerHTML = '';
                        if (!entries.length) {
                            timelineEl.innerHTML =
                                '<div class="timeline-empty"><i class="fas fa-history"></i>No history yet.</div>';
                            if (pagEl) pagEl.style.display = 'none';
                            return;
                        }
                        entries.forEach(entry => {
                            const item = document.createElement('div');
                            item.className = 'timeline-item ' + getTimelineClass(entry.reason || '');
                            let tsLabel = 'Time unknown';
                            if (entry.timestamp != null) {
                                tsLabel = new Date(entry.timestamp * 1000).toLocaleString('en-US', {
                                    weekday: 'short', year: 'numeric', month: 'short',
                                    day: 'numeric', hour: '2-digit', minute: '2-digit',
                                    second: '2-digit', hour12: true,
                                });
                            }
                            item.innerHTML =
                                `<div class="tl-msg">${formatMessageForDisplay(entry.reason)}</div>` +
                                `<div class="tl-time">${tsLabel}</div>`;
                            timelineEl.appendChild(item);
                        });
                        const total = _workerHistoryTotal;
                        const pageSize = data.page_size || WORKER_HISTORY_PAGE_SIZE;
                        const start = _workerHistoryPage * pageSize + 1;
                        const end = Math.min(start + entries.length - 1, total);
                        const totalPages = data.total_pages
                            || Math.max(1, Math.ceil(total / pageSize));
                        if (pagEl) {
                            pagEl.style.display = total > pageSize ? 'flex' : 'none';
                        }
                        if (infoEl) {
                            infoEl.textContent =
                                `Showing ${start}–${end} of ${total} (newest first)`;
                        }
                        if (prevBtn) prevBtn.disabled = _workerHistoryPage <= 0;
                        if (nextBtn) nextBtn.disabled = _workerHistoryPage >= totalPages - 1;
                    })
                    .catch(err => {
                        timelineEl.innerHTML =
                            `<div class="timeline-empty"><i class="fas fa-exclamation-triangle"></i>${_escHtml(err.message)}</div>`;
                    })
                    .finally(() => { _workerHistoryLoading = false; });
            }

            function changeWorkerHistoryPage(direction) {
                if (!_workerDetailCache) return;
                loadWorkerHistoryPage(_workerHistoryPage + direction);
            }

            function floatOrZero(v) {
                const n = parseFloat(v);
                return Number.isFinite(n) ? n : 0;
            }

            function _formatWorkerPollTime(ts) {
                if (ts == null || ts === '' || ts === 'N/A') return '—';
                const n = typeof ts === 'number' ? ts : parseFloat(ts);
                if (!Number.isFinite(n) || n <= 0) return '—';
                try {
                    return new Date(n * 1000).toLocaleString(undefined, {
                        weekday: 'short',
                        year: 'numeric',
                        month: 'short',
                        day: 'numeric',
                        hour: '2-digit',
                        minute: '2-digit',
                        second: '2-digit',
                        hour12: true,
                    });
                } catch (e) {
                    return '—';
                }
            }

            function _formatWorkerHistoryEventLabel(entry) {
                const ts = entry.timestamp
                    ? new Date(entry.timestamp * 1000).toLocaleString('en-US', {
                        weekday: 'short', year: 'numeric', month: 'short',
                        day: 'numeric', hour: '2-digit', minute: '2-digit',
                        second: '2-digit', hour12: true,
                    })
                    : 'unknown time';
                const event = entry.event || 'event';
                const reason = (entry.reason || '').trim();
                const shortReason = reason.length > 80 ? reason.slice(0, 77) + '…' : reason;
                return shortReason
                    ? `${event} — ${ts} — ${shortReason}`
                    : `${event} — ${ts}`;
            }

            function _getWorkerHistoryMetricsCount() {
                return _workerMetricsHistoryTotal;
            }

            function _updateMetricsSnapshotTime(entry) {
                const el = document.getElementById('workerMetricsSnapshotTime');
                if (!el) return;
                const ts = entry?.timestamp ?? _workerDetailCache?.last_poll_at;
                el.textContent = ts ? _formatWorkerPollTime(ts) : '—';
            }

            function _showLiveWorkerMetricsFallback() {
                const panel = document.getElementById('workerMetricsPanel');
                const controlsEl = document.getElementById('workerHistoryMetricsControls');
                const labelEl = document.getElementById('workerHistoryMetricsEventLabel');
                const infoEl = document.getElementById('workerHistoryMetricsPaginationInfo');
                const w = _workerDetailCache;
                if (!panel || !w) return;

                const sm = w.system_metrics;
                if (!sm || !Object.keys(sm).length) {
                    panel.innerHTML = '<p class="worker-metrics-empty">No metrics recorded yet.</p>';
                    if (controlsEl) controlsEl.style.display = 'none';
                    if (labelEl) {
                        labelEl.style.display = 'none';
                        labelEl.textContent = '';
                    }
                    if (infoEl) infoEl.textContent = '';
                    _updateMetricsSnapshotTime(null);
                    return;
                }

                _workerMetricsHistoryEntry = {
                    metrics: sm,
                    timestamp: w.last_poll_at,
                    event: 'heartbeat',
                };
                if (labelEl) {
                    labelEl.style.display = 'block';
                    labelEl.textContent = 'Latest heartbeat (no metrics history snapshots yet).';
                }
                if (controlsEl) controlsEl.style.display = 'none';
                if (infoEl) infoEl.textContent = '';
                _updateMetricsSnapshotTime(_workerMetricsHistoryEntry);
                renderSystemMetrics(panel, sm);
            }

            function loadWorkerMetricsView(silent) {
                const panel = document.getElementById('workerMetricsPanel');
                if (!_workerDetailCache || !panel) {
                    return Promise.resolve();
                }
                return loadWorkerMetricsHistoryIndex(_workerMetricsHistoryIndex, silent).then(() => {
                    if (_workerMetricsHistoryTotal === 0) {
                        _showLiveWorkerMetricsFallback();
                    }
                });
            }

            function loadWorkerMetricsHistoryIndex(index, silent) {
                const panel = document.getElementById('workerMetricsPanel');
                if (!_workerDetailCache || !panel) {
                    return Promise.resolve();
                }
                if (!silent && _workerMetricsHistoryLoading) {
                    return Promise.resolve();
                }

                const fetchGen = ++_workerMetricsFetchGen;
                if (!silent) {
                    _workerMetricsHistoryLoading = true;
                    if (!panel.querySelector('.metrics-panel[data-metrics-ready]')) {
                        panel.innerHTML = '<p class="worker-metrics-empty">Loading metrics snapshot…</p>';
                    }
                }

                return _workerHistoryQuery(index, 1, true)
                    .then(data => {
                        if (fetchGen !== _workerMetricsFetchGen) return;
                        _workerMetricsHistoryIndex = data.page ?? index;
                        _workerMetricsHistoryTotal = data.total ?? 0;
                        _workerMetricsHistoryEntry = (data.entries || [])[0] || null;
                        renderWorkerMetricsView(silent);
                    })
                    .catch(err => {
                        if (fetchGen !== _workerMetricsFetchGen) return;
                        stopWorkerHistoryMetricsPlayback();
                        _workerMetricsHistoryEntry = null;
                        _workerMetricsHistoryTotal = 0;
                        panel.innerHTML =
                            `<p class="worker-metrics-empty" style="color:#dc3545;">${_escHtml(err.message)}</p>`;
                        const controlsEl = document.getElementById('workerHistoryMetricsControls');
                        if (controlsEl) controlsEl.style.display = 'none';
                        _updateMetricsSnapshotTime(null);
                    })
                    .finally(() => {
                        if (!silent && fetchGen === _workerMetricsFetchGen) {
                            _workerMetricsHistoryLoading = false;
                        }
                    });
            }

            function stopWorkerHistoryMetricsPlayback() {
                if (_workerMetricsPlayTimer) {
                    clearInterval(_workerMetricsPlayTimer);
                    _workerMetricsPlayTimer = null;
                }
                const icon = document.getElementById('workerHistoryMetricsPlayPauseIcon');
                const label = document.getElementById('workerHistoryMetricsPlayPauseLabel');
                const btn = document.getElementById('workerHistoryMetricsPlayPause');
                if (icon) icon.className = 'fas fa-play';
                if (label) label.textContent = 'Play';
                if (btn) btn.setAttribute('aria-pressed', 'false');
            }

            function _advanceWorkerHistoryMetricsSnapshot() {
                const total = _getWorkerHistoryMetricsCount();
                if (total <= 1) return;
                let next = _workerMetricsHistoryIndex + 1;
                if (next >= total) next = 0;
                loadWorkerMetricsHistoryIndex(next, true);
            }

            function toggleWorkerHistoryMetricsPlayback() {
                if (_workerMetricsPlayTimer) {
                    stopWorkerHistoryMetricsPlayback();
                    renderWorkerMetricsView(true);
                    return;
                }
                const startTimer = () => {
                    if (_workerMetricsHistoryTotal <= 1) return;
                    const icon = document.getElementById('workerHistoryMetricsPlayPauseIcon');
                    const label = document.getElementById('workerHistoryMetricsPlayPauseLabel');
                    const btn = document.getElementById('workerHistoryMetricsPlayPause');
                    if (icon) icon.className = 'fas fa-pause';
                    if (label) label.textContent = 'Pause';
                    if (btn) btn.setAttribute('aria-pressed', 'true');
                    _workerMetricsPlayTimer = setInterval(
                        _advanceWorkerHistoryMetricsSnapshot,
                        WORKER_METRICS_PLAY_INTERVAL_MS,
                    );
                };
                loadWorkerMetricsHistoryIndex(0).then(startTimer);
            }

            function renderWorkerMetricsView(silentUpdate) {
                const labelEl = document.getElementById('workerHistoryMetricsEventLabel');
                const controlsEl = document.getElementById('workerHistoryMetricsControls');
                const infoEl = document.getElementById('workerHistoryMetricsPaginationInfo');
                const playBtn = document.getElementById('workerHistoryMetricsPlayPause');
                const panel = document.getElementById('workerMetricsPanel');
                if (!panel) return;

                const entry = _workerMetricsHistoryEntry;
                const total = _workerMetricsHistoryTotal;
                if (!entry || !entry.metrics || !Object.keys(entry.metrics).length) {
                    stopWorkerHistoryMetricsPlayback();
                    if (total === 0 && !_workerMetricsHistoryLoading) {
                        _showLiveWorkerMetricsFallback();
                    }
                    return;
                }

                if (labelEl) {
                    labelEl.style.display = 'block';
                    labelEl.textContent = _formatWorkerHistoryEventLabel(entry);
                }
                _updateMetricsSnapshotTime(entry);
                renderSystemMetrics(panel, entry.metrics);

                const pos = _workerMetricsHistoryIndex + 1;
                if (controlsEl) {
                    controlsEl.style.display = total > 1 ? 'flex' : 'none';
                }
                if (infoEl) {
                    const playing = Boolean(_workerMetricsPlayTimer);
                    infoEl.textContent = playing
                        ? `Playing snapshot ${pos} of ${total} (newest first)`
                        : `Snapshot ${pos} of ${total} (newest first)`;
                }
                if (playBtn) {
                    playBtn.disabled = total <= 1;
                }
                const prevBtn = document.getElementById('workerHistoryMetricsPrev');
                const nextBtn = document.getElementById('workerHistoryMetricsNext');
                if (prevBtn) prevBtn.disabled = _workerMetricsHistoryIndex <= 0;
                if (nextBtn) nextBtn.disabled = _workerMetricsHistoryIndex >= total - 1;
            }

            function changeWorkerHistoryMetricsPage(direction) {
                if (!_workerDetailCache) return;
                stopWorkerHistoryMetricsPlayback();
                const next = _workerMetricsHistoryIndex + direction;
                if (next < 0 || next >= _workerMetricsHistoryTotal) return;
                loadWorkerMetricsHistoryIndex(next, false);
            }

            document.addEventListener('DOMContentLoaded', function() {
                loadWorkerSummary();
                setInterval(loadWorkerSummary, 30000);
            });

// Pagination and Search Variables
            const JOB_STATUSES = ['SERVED', 'DONE', 'ABORTED', 'PENDING', 'DELETED'];
            let _jobPageSize = 50;
            let currentPages = {
                'SERVED': 1,
                'DONE': 1,
                'ABORTED': 1,
                'PENDING': 1,
                'DELETED': 1,
            };
            let currentSearchJobId = null;
            let currentStatus = 'SERVED';
            const _jobPageRowsByStatus = {};
            const _jobListTotalByStatus = {};
            const _jobSelectedIdsByStatus = {};
            let _jobSearchDebounce = {};
            let _jobActionPreviewState = null;

            JOB_STATUSES.forEach(s => { _jobSelectedIdsByStatus[s] = new Set(); });

            function _jobTabSupportsSelection(status) {
                return status !== 'SERVED';
            }

            function _jobBulkActionsForTab(status) {
                if (status === 'PENDING') {
                    return [
                        { action: 'delete', label: 'Delete', cls: 'workers-btn--stop' },
                        { action: 'to_done', label: 'Mark DONE', cls: 'workers-btn--run' },
                    ];
                }
                if (status === 'DONE' || status === 'ABORTED') {
                    return [{ action: 'to_pending', label: 'Reset to PENDING', cls: 'workers-btn--pause' }];
                }
                if (status === 'DELETED') {
                    return [{ action: 'restore', label: 'Restore to PENDING', cls: 'workers-btn--run' }];
                }
                return [];
            }

            function _jobActionLabel(action) {
                return {
                    delete: 'Delete',
                    restore: 'Restore to PENDING',
                    to_pending: 'Reset to PENDING',
                    to_done: 'Mark DONE',
                }[action] || action;
            }

            function clearJobSelection(status, refreshBar) {
                if (!_jobSelectedIdsByStatus[status]) return;
                _jobSelectedIdsByStatus[status].clear();
                if (refreshBar !== false) {
                    _updateJobBulkBar(status);
                    _syncJobPageCheckboxes(status);
                }
            }

            function clearAllJobSelections() {
                JOB_STATUSES.forEach(s => clearJobSelection(s, false));
            }

            function toggleJobSelection(status, jobId, checked) {
                if (!jobId || !_jobSelectedIdsByStatus[status]) return;
                const id = parseInt(jobId, 10);
                if (checked) {
                    _jobSelectedIdsByStatus[status].add(id);
                } else {
                    _jobSelectedIdsByStatus[status].delete(id);
                }
                _updateJobBulkBar(status);
                _syncJobPageCheckboxes(status);
            }

            function toggleJobSelectAllPage(status, checked) {
                if (!_jobTabSupportsSelection(status)) return;
                (_jobPageRowsByStatus[status] || []).forEach(job => {
                    const id = parseInt(job.id, 10);
                    if (checked) {
                        _jobSelectedIdsByStatus[status].add(id);
                    } else {
                        _jobSelectedIdsByStatus[status].delete(id);
                    }
                });
                _updateJobBulkBar(status);
                _syncJobPageCheckboxes(status);
            }

            function _syncJobPageCheckboxes(status) {
                const checkAll = document.getElementById(`jobsCheckAllPage-${status}`);
                const thCheck = document.getElementById(`jobsCheckAllTh-${status}`);
                const selectable = _jobTabSupportsSelection(status);
                if (thCheck) thCheck.style.display = '';
                const rows = _jobPageRowsByStatus[status] || [];
                const selected = _jobSelectedIdsByStatus[status];
                if (checkAll) {
                    checkAll.disabled = !selectable;
                    if (!selectable) {
                        checkAll.checked = false;
                        checkAll.indeterminate = false;
                    } else {
                        const pageIds = rows.map(j => parseInt(j.id, 10));
                        const onPage = pageIds.filter(id => selected.has(id)).length;
                        checkAll.checked = pageIds.length > 0 && onPage === pageIds.length;
                        checkAll.indeterminate = onPage > 0 && onPage < pageIds.length;
                    }
                }
                document.querySelectorAll(`.jobs-row-check[data-status="${status}"]`).forEach(cb => {
                    const id = parseInt(cb.dataset.jobId, 10);
                    cb.checked = selected.has(id);
                });
            }

            function _updateJobBulkBar(status) {
                const bar = document.getElementById(`jobsBulkBar-${status}`);
                const countEl = document.getElementById(`jobsBulkCount-${status}`);
                const chipsEl = document.getElementById(`jobsBulkChips-${status}`);
                const actionsEl = document.getElementById(`jobsBulkActions-${status}`);
                const selected = _jobSelectedIdsByStatus[status];
                const n = selected?.size || 0;
                if (!bar || !countEl || !actionsEl) return;
                if (!n || !_jobTabSupportsSelection(status)) {
                    bar.style.display = 'none';
                    if (chipsEl) chipsEl.innerHTML = '';
                    return;
                }
                bar.style.display = 'flex';

                const ids = Array.from(selected).sort((a, b) => a - b);
                const pageIds = new Set(
                    (_jobPageRowsByStatus[status] || []).map(j => parseInt(j.id, 10)),
                );
                const onPage = ids.filter(id => pageIds.has(id)).length;
                const offPage = n - onPage;
                countEl.textContent = offPage > 0
                    ? `${n} selected · ${offPage} not on this page`
                    : `${n} selected`;

                if (chipsEl) {
                    const maxChips = 15;
                    const visible = ids.slice(0, maxChips);
                    const overflow = ids.length - visible.length;
                    chipsEl.innerHTML =
                        visible.map(id =>
                            `<button type="button" class="jobs-selection-chip" title="Remove #${id} from selection" onclick="toggleJobSelection('${status}', '${id}', false)">#${id}<span class="jobs-selection-chip-x">&times;</span></button>`,
                        ).join('') +
                        (overflow > 0
                            ? `<span class="jobs-selection-chip jobs-selection-chip--more">+${overflow} more</span>`
                            : '');
                }

                actionsEl.innerHTML = _jobBulkActionsForTab(status).map(a =>
                    `<button type="button" class="workers-btn-sm ${a.cls}" onclick="requestJobAction('${a.action}','jobs','${status}')">${a.label} selected</button>`,
                ).join('');
            }

            function onJobSearchInput(status) {
                const input = document.getElementById(`jobSearch-${status}`);
                _syncSearchClearBtn(input);
                clearTimeout(_jobSearchDebounce[status]);
                _jobSearchDebounce[status] = setTimeout(() => {
                    currentPages[status] = 1;
                    const q = document.getElementById(`jobSearch-${status}`)?.value?.trim() || '';
                    loadJobs(status, 1, q || null);
                }, 280);
            }

            function _jobSearchQuery(status) {
                return document.getElementById(`jobSearch-${status}`)?.value?.trim() || '';
            }

            function _updateJobsSelectAllMatchingBtn(status) {
                const btn = document.getElementById(`jobsSelectAllMatchingBtn-${status}`);
                if (!btn) return;
                const search = _jobSearchQuery(status);
                const total = _jobListTotalByStatus[status] || 0;
                if (!search || !total || !_jobTabSupportsSelection(status)) {
                    btn.style.display = 'none';
                    return;
                }
                btn.style.display = 'inline';
                btn.textContent = `Add all ${total} matching to selection`;
                btn.disabled = false;
            }

            async function selectAllMatchingJobs(status) {
                const search = _jobSearchQuery(status);
                if (!search || !_jobTabSupportsSelection(status)) return;
                const btn = document.getElementById(`jobsSelectAllMatchingBtn-${status}`);
                if (btn) btn.disabled = true;
                try {
                    let page = 1;
                    let totalPages = 1;
                    let added = 0;
                    while (page <= totalPages) {
                        const params = new URLSearchParams({
                            page: String(page),
                            per_page: String(_jobPageSize),
                            status: status,
                            search_job_id: search,
                        });
                        const r = await fetch(`/jobs_paginated?${params}`);
                        const data = await r.json();
                        if (data.error) throw new Error(data.error);
                        totalPages = data.total_pages || 1;
                        (data.jobs || []).forEach(j => {
                            _jobSelectedIdsByStatus[status].add(parseInt(j.id, 10));
                        });
                        added += (data.jobs || []).length;
                        page += 1;
                    }
                    showNotification(`Added ${added} job(s) to selection.`, 'info');
                    _updateJobBulkBar(status);
                    _syncJobPageCheckboxes(status);
                } catch (e) {
                    showNotification('Failed to select jobs: ' + e.message, 'error');
                } finally {
                    if (btn) btn.disabled = false;
                }
            }

            function _buildJobRowQuickActions(status, jobId) {
                const actions = _jobBulkActionsForTab(status);
                if (!actions.length) return '';
                return actions.map(a =>
                    `<button type="button" class="workers-btn-sm ${a.cls}" onclick="requestJobAction('${a.action}','job','${status}',${jobId})">${a.label}</button>`,
                ).join('');
            }

            function _buildJobRowActions(status, jobId) {
                return `<button type="button" class="workers-btn-sm" onclick="openJobDetails(${jobId})">Details</button>${_buildJobRowQuickActions(status, jobId)}`;
            }

            function _jobsEmptyTableRow(status, message) {
                const check = _jobTabSupportsSelection(status)
                    ? '<td class="jobs-td-check"></td>'
                    : _disabledCheckboxTd('jobs-td-check');
                return `<tr class="table-empty-row">${check}<td colspan="6" class="table-empty-cell">${message}</td></tr>`;
            }

            function _jobsErrorTableRow(status, message) {
                return `<tr class="table-empty-row table-error-row">${_disabledCheckboxTd('jobs-td-check')}<td colspan="6" class="table-empty-cell table-error-cell">${_escHtml(message)}</td></tr>`;
            }

            function _jobsTableOverlayHtml(kind, status, message) {
                if (kind === 'loading') {
                    return '<div class="loading-message"><i class="fas fa-spinner fa-spin"></i> Loading jobs...</div>';
                }
                const isError = kind === 'error';
                const title = isError ? 'Error Loading Jobs' : 'No jobs found';
                const sub = message || (isError ? 'Please try again.' : `No ${status.toLowerCase()} jobs available`);
                return `<div class="empty-state${isError ? ' error-state' : ''}">
                    <div class="empty-icon"><i class="fas fa-${isError ? 'exclamation-triangle' : 'database'}"></i></div>
                    <div class="empty-text">${title}</div>
                    <div class="empty-subtext">${sub}</div>
                </div>`;
            }

            function _setJobsTableOverlay(status, html, visible) {
                const overlay = document.getElementById(`jobs-table-overlay-${status}`);
                const tbody = document.getElementById(`tbody-${status}`);
                if (!overlay) return;
                overlay.innerHTML = html;
                overlay.style.display = visible ? 'flex' : 'none';
                if (visible && tbody) tbody.innerHTML = '';
            }

            function _jobActionScopeLabel(action, scope, status, targetJobId) {
                const label = _jobActionLabel(action);
                if (scope === 'jobs') {
                    return `${label} ${_jobSelectedIdsByStatus[status]?.size || 0} selected job(s)`;
                }
                if (scope === 'all') {
                    const search = document.getElementById(`jobSearch-${status}`)?.value?.trim();
                    const searchNote = search ? ` matching "${search}"` : '';
                    return `${label} all ${status} jobs${searchNote}`;
                }
                return `${label} job #${targetJobId}`;
            }

            function _renderJobPreviewList(jobs) {
                if (!jobs.length) {
                    return '<p class="workers-action-preview-empty">No jobs would be affected.</p>';
                }
                const rows = jobs.map(j => `<tr>
                    <td><strong>#${j.id}</strong></td>
                    <td>${_escHtml(j.worker_id || '—')}</td>
                    <td>${_escHtml(j.status || '—')}</td>
                </tr>`).join('');
                return `<table class="workers-action-preview-table">
                    <thead><tr><th>Job ID</th><th>Worker</th><th>Status</th></tr></thead>
                    <tbody>${rows}</tbody>
                </table>`;
            }

            function closeJobActionPreviewModal() {
                const modal = document.getElementById('jobActionPreviewModal');
                if (modal) modal.style.display = 'none';
                _jobActionPreviewState = null;
            }

            function _refreshJobsAfterBulk(status) {
                const searchInput = document.getElementById(`jobSearch-${status}`);
                const searchJobId = searchInput?.value?.trim() || null;
                clearJobSelection(status);
                loadJobs(status, currentPages[status] || 1, searchJobId);
                JOB_STATUSES.forEach(s => {
                    if (s !== status) loadJobs(s, currentPages[s] || 1,
                        document.getElementById(`jobSearch-${s}`)?.value?.trim() || null);
                });
                setTimeout(() => location.reload(), 800);
            }

            function confirmJobActionPreview() {
                const st = _jobActionPreviewState;
                if (!st) return;
                const btn = document.getElementById('jobActionPreviewConfirm');
                const reason = (document.getElementById('jobActionPreviewReason')?.value || '').trim();
                if (btn) btn.disabled = true;
                fetch('/jobs/bulk_action', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ ...st.payload, reason }),
                })
                    .then(r => r.json().then(d => ({ ok: r.ok, data: d })))
                    .then(({ ok, data }) => {
                        if (btn) btn.disabled = false;
                        closeJobActionPreviewModal();
                        if (!ok || !data.success) {
                            showNotification(data.error || 'Action failed.', 'error');
                            return;
                        }
                        const label = _jobActionLabel(st.action);
                        let msg = `${label}: ${data.affected} job(s) updated.`;
                        if (data.failed?.length) {
                            msg += ` ${data.failed.length} failed.`;
                        }
                        showNotification(msg, data.failed?.length ? 'warning' : 'success');
                        closeMessageModal();
                        _refreshJobsAfterBulk(st.status);
                    })
                    .catch(e => {
                        if (btn) btn.disabled = false;
                        showNotification('Network error: ' + e.message, 'error');
                    });
            }

            function requestJobAction(action, scope, status, targetJobId, prefillReason) {
                status = status || currentStatus;
                if (scope === 'jobs' && (!_jobSelectedIdsByStatus[status] || _jobSelectedIdsByStatus[status].size === 0)) {
                    showNotification('Select at least one job.', 'warning');
                    return;
                }
                const searchInput = document.getElementById(`jobSearch-${status}`);
                const searchJobId = searchInput?.value?.trim() || null;
                const payload = {
                    action,
                    scope,
                    status,
                    search_job_id: searchJobId,
                };
                if (scope === 'job') {
                    payload.target = parseInt(targetJobId, 10);
                } else if (scope === 'jobs') {
                    payload.job_ids = Array.from(_jobSelectedIdsByStatus[status]);
                }

                fetch('/jobs/preview', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(payload),
                })
                    .then(r => r.json().then(d => ({ ok: r.ok, data: d })))
                    .then(({ ok, data }) => {
                        if (!ok || !data.success) {
                            showNotification(data.error || 'Preview failed.', 'error');
                            return;
                        }
                        const jobs = data.jobs || [];
                        const count = data.count ?? jobs.length;
                        const modal = document.getElementById('jobActionPreviewModal');
                        const title = document.getElementById('jobActionPreviewTitle');
                        const summary = document.getElementById('jobActionPreviewSummary');
                        const list = document.getElementById('jobActionPreviewList');
                        const confirmBtn = document.getElementById('jobActionPreviewConfirm');
                        const reasonInput = document.getElementById('jobActionPreviewReason');
                        if (!modal || !title || !summary || !list) return;

                        title.textContent = count
                            ? `Confirm: ${_jobActionScopeLabel(action, scope, status, targetJobId)}`
                            : 'No jobs affected';
                        summary.textContent = count
                            ? `${count} job(s) will be updated. The reason is recorded in each job's history.`
                            : `No jobs match this action on the ${status} tab.`;
                        list.innerHTML = _renderJobPreviewList(jobs);
                        if (reasonInput) {
                            reasonInput.value = (prefillReason != null && prefillReason !== '')
                                ? String(prefillReason) : '';
                        }

                        if (confirmBtn) {
                            confirmBtn.textContent = count
                                ? `Confirm ${_jobActionLabel(action)}`
                                : 'Close';
                            confirmBtn.className = 'workers-btn ' + (
                                action === 'delete' ? 'workers-btn--stop' : 'workers-btn--run'
                            );
                            confirmBtn.onclick = count
                                ? () => confirmJobActionPreview()
                                : () => closeJobActionPreviewModal();
                        }

                        _jobActionPreviewState = count
                            ? { action, payload, status, scope, targetJobId }
                            : null;
                        modal.style.display = 'block';
                    })
                    .catch(e => showNotification('Network error: ' + e.message, 'error'));
            }

            // Enhanced encoding with validation - Define these functions first
            function encodeForHtmlAttribute(text) {
                try {
                    console.log('encodeForHtmlAttribute input:', text);
                    console.log('encodeForHtmlAttribute input type:', typeof text);
                    
                    text = safeStringify(text === undefined ? null : text);
                    console.log('After safeStringify:', text);
                    
                    if (!text || text === 'null' || text === 'undefined') {
                        console.log('Empty/null/undefined, returning empty string');
                        return '';
                    }
                    
                    // Handle essential characters for onclick + preserve newlines
                    const result = text
                        .replace(/&/g, '&amp;')           // Ampersand first
                        .replace(/"/g, '&quot;')          // Double quote
                        .replace(/'/g, '&#39;')           // Single quote
                        .replace(/</g, '&lt;')            // Less than
                        .replace(/>/g, '&gt;')            // Greater than
                        .replace(/\\n/g, '&#10;')          // Newline
                        .replace(/\\r/g, '&#13;');         // Carriage return
                    
                    console.log('encodeForHtmlAttribute result:', result);
                    return result;
                } catch (error) {
                    console.error('Error in encodeForHtmlAttribute:', error);
                    return '';
                }
            }

            function decodeFromHtmlAttribute(text) {
                console.log('decodeFromHtmlAttribute input:', text);
                console.log('decodeFromHtmlAttribute input type:', typeof text);
                
                if (typeof text !== 'string') {
                    console.log('Not a string, returning as-is');
                    return text;
                }
                
                const result = text
                    .replace(/&quot;/g, '"')          // Double quote
                    .replace(/&#39;/g, "'")            // Single quote
                    .replace(/&amp;/g, '&')           // Ampersand
                    .replace(/&lt;/g, '<')            // Less than
                    .replace(/&gt;/g, '>')            // Greater than
                    .replace(/&#10;/g, '\\n')          // Newline
                    .replace(/&#13;/g, '\\r');         // Carriage return
                
                console.log('decodeFromHtmlAttribute result:', result);
                return result;
            }

            function safeStringify(obj, fallback = '{}') {
                try {
                    return JSON.stringify(obj);
                } catch (error) {
                    console.error('Error stringifying object:', error);
                    return fallback;
                }
            }

            function safeJsonParse(value, fallback = null) {
                try {
                    if (value === null || value === undefined) {
                        return fallback;
                    }
                    if (typeof value === 'object') {
                        return value;
                    }

                    console.log('safeJsonParse input:', value);
                    console.log('safeJsonParse input type:', typeof value);
                    
                    // First decode HTML entities
                    const decoded = decodeFromHtmlAttribute(value);
                    console.log('After decodeFromHtmlAttribute:', decoded);
                    
                    // Clean up control characters that break JSON parsing
                    const cleaned = decoded
                        .replace(/\\n/g, '\\\\n')      // Escape newlines for JSON
                        .replace(/\\r/g, '\\\\r')      // Escape carriage returns for JSON
                        .replace(/\\t/g, '\\\\t');     // Escape tabs for JSON
                    
                    console.log('After cleaning control characters:', cleaned);
                    
                    // Then parse JSON
                    const result = JSON.parse(cleaned);
                    console.log('After JSON.parse:', result);
                    return result;
                } catch (error) {
                    console.error('JSON parsing error:', error);
                    console.error('Original string:', value);
                    return fallback;
                }
            }

            function formatMessageForDisplay(message) {
                if (typeof message === 'string') {
                    // Convert newlines to HTML line breaks
                    return message.replace(/\\n/g, '<br>');
                }
                return String(message);
            }

            function normalizeHistoryEntries(entries) {
                const out = [];
                if (!Array.isArray(entries)) {
                    entries = [entries];
                }
                for (const entry of entries) {
                    if (entry && typeof entry === 'object' && !Array.isArray(entry)) {
                        out.push({
                            reason: entry.reason != null ? String(entry.reason) : JSON.stringify(entry),
                            timestamp: (typeof entry.timestamp === 'number' && !Number.isNaN(entry.timestamp))
                                ? entry.timestamp : null,
                        });
                    } else if (typeof entry === 'string') {
                        out.push({ reason: entry, timestamp: null });
                    } else if (entry != null) {
                        out.push({ reason: String(entry), timestamp: null });
                    }
                }
                return out;
            }

            /** Parse job audit history — never throws; tolerates plain strings and bad JSON. */
            function parseJobHistory(raw) {
                if (raw === null || raw === undefined || raw === '') {
                    return [];
                }
                if (Array.isArray(raw)) {
                    return normalizeHistoryEntries(raw);
                }

                let text = raw;
                if (typeof text !== 'string') {
                    if (typeof text === 'object') {
                        return normalizeHistoryEntries([text]);
                    }
                    text = String(text);
                }

                text = decodeFromHtmlAttribute(text);
                if (!text || text === 'null' || text === 'undefined') {
                    return [];
                }
                text = text.trim();
                if (!text) {
                    return [];
                }

                // Direct JSON parse (preferred — avoids mangling valid JSON)
                try {
                    const parsed = JSON.parse(text);
                    if (Array.isArray(parsed)) {
                        return normalizeHistoryEntries(parsed);
                    }
                    if (parsed && typeof parsed === 'object') {
                        return normalizeHistoryEntries([parsed]);
                    }
                    if (typeof parsed === 'string' && parsed.trim()) {
                        return [{ reason: parsed, timestamp: null }];
                    }
                } catch (directErr) {
                    // fall through
                }

                // Legacy: single-quoted or partially escaped payloads from old rows
                const fallback = safeJsonParse(text, null);
                if (Array.isArray(fallback)) {
                    return normalizeHistoryEntries(fallback);
                }
                if (fallback && typeof fallback === 'object') {
                    return normalizeHistoryEntries([fallback]);
                }
                if (typeof fallback === 'string' && fallback.trim()) {
                    return [{ reason: fallback, timestamp: null }];
                }

                // Plain text stored instead of a JSON array
                return [{ reason: text, timestamp: null }];
            }

            function renderJobHistory(timelineEl, rawMessage) {
                timelineEl.innerHTML = '';
                let entries;
                try {
                    entries = parseJobHistory(rawMessage);
                } catch (err) {
                    console.error('parseJobHistory failed:', err, rawMessage);
                    entries = [{ reason: String(rawMessage || ''), timestamp: null }];
                }

                if (!entries.length) {
                    timelineEl.innerHTML =
                        '<div class="timeline-empty"><i class="fas fa-history"></i>No history yet — audit entries appear here when actions are taken on this job.</div>';
                    return;
                }

                entries.slice().reverse().forEach(entry => {
                    const item = document.createElement('div');
                    item.className = 'timeline-item ' + getTimelineClass(entry.reason || '');

                    let tsLabel = 'Time unknown';
                    if (entry.timestamp != null) {
                        tsLabel = new Date(entry.timestamp * 1000).toLocaleString('en-US', {
                            weekday: 'short', year: 'numeric', month: 'short',
                            day: 'numeric', hour: '2-digit', minute: '2-digit',
                            second: '2-digit', hour12: true
                        });
                    }

                    item.innerHTML =
                        `<div class="tl-msg">${formatMessageForDisplay(entry.reason)}</div>` +
                        `<div class="tl-time">${tsLabel}</div>`;
                    timelineEl.appendChild(item);
                });
            }

            // Load jobs for a specific status and page
            function loadJobs(status, page = 1, searchJobId = null) {
                const tbody = document.getElementById(`tbody-${status}`);
                _setJobsTableOverlay(status, _jobsTableOverlayHtml('loading', status), true);

                const params = new URLSearchParams({
                    page: page,
                    per_page: _jobPageSize,
                    status: status
                });

                if (searchJobId) {
                    params.append('search_job_id', searchJobId);
                }

                fetch(`/jobs_paginated?${params}`)
                    .then(response => response.json())
                    .then(data => {
                        if (data.error) {
                            _jobPageRowsByStatus[status] = [];
                            _setJobsTableOverlay(status, '', false);
                            tbody.innerHTML = _jobsErrorTableRow(status, data.error);
                            _updateJobBulkBar(status);
                            _updateJobsSelectAllMatchingBtn(status);
                            return;
                        }

                        currentPages[status] = data.current_page || page;

                        // Update pagination info
                        document.getElementById(`page-info-${status}`).textContent = `Page ${data.current_page} of ${data.total_pages}`;
                        document.getElementById(`prev-${status}`).disabled = data.current_page <= 1;
                        document.getElementById(`next-${status}`).disabled = data.current_page >= data.total_pages;

                        // Update pagination info for this status
                        document.getElementById(`paginationInfo-${status}`).textContent = 
                            `Showing ${((data.current_page - 1) * data.per_page) + 1}-${Math.min(data.current_page * data.per_page, data.total_count)} of ${data.total_count} jobs`;

                        _jobPageRowsByStatus[status] = data.jobs;
                        _jobListTotalByStatus[status] = data.total_count || 0;

                        // Render jobs
                        if (data.jobs.length === 0) {
                            _jobPageRowsByStatus[status] = [];
                            _setJobsTableOverlay(status, '', false);
                            const emptyMsg = _jobSearchQuery(status)
                                ? `No ${status.toLowerCase()} jobs match your search.`
                                : `No ${status.toLowerCase()} jobs available.`;
                            tbody.innerHTML = _jobsEmptyTableRow(status, emptyMsg);
                            _syncJobPageCheckboxes(status);
                            _updateJobBulkBar(status);
                            _updateJobsSelectAllMatchingBtn(status);
                            return;
                        }

                        _setJobsTableOverlay(status, '', false);

                        const selectable = _jobTabSupportsSelection(status);
                        const selected = _jobSelectedIdsByStatus[status];
                        let html = '';
                        data.jobs.forEach(job => {
                            const workerId       = job.worker_id || 'Unassigned';
                            const reqTs          = job.request_timestamp    || 0;
                            const compTs         = job.completion_timestamp || 0;
                            const reqTime        = reqTs  ? new Date(reqTs  * 1000).toLocaleString() : '';
                            const compTime       = compTs ? new Date(compTs * 1000).toLocaleString() : '';
                            const durationSec    = job.required_time || 0;
                            const durationFmt    = durationSec ? formatTime(durationSec) : '';
                            const jid = parseInt(job.id, 10);
                            const checked = selected.has(jid) ? ' checked' : '';
                            const checkCell = selectable
                                ? `<td class="jobs-td-check"><input type="checkbox" class="jobs-row-check" data-status="${status}" data-job-id="${jid}"${checked} onchange="toggleJobSelection('${status}', this.dataset.jobId, this.checked)" aria-label="Select job ${jid}"></td>`
                                : _disabledCheckboxTd('jobs-td-check');
                            const rowActions = _buildJobRowActions(status, jid);

                            html += `
                                <tr>
                                    ${checkCell}
                                    <td data-value="${job.id}" class="jobs-td-id">${job.id}</td>
                                    <td class="jobs-td-worker">${_escHtml(workerId)}</td>
                                    <td data-value="${reqTs}">${reqTime}</td>
                                    <td data-value="${compTs}">${compTime}</td>
                                    <td data-value="${durationSec}">${durationFmt}</td>
                                    <td class="jobs-row-actions">${rowActions}</td>
                                </tr>
                            `;
                        });
                        tbody.innerHTML = html;
                        _syncJobPageCheckboxes(status);
                        _updateJobBulkBar(status);
                        _updateJobsSelectAllMatchingBtn(status);
                    })
                    .catch(error => {
                        console.error('Error loading jobs:', error);
                        _jobPageRowsByStatus[status] = [];
                        _setJobsTableOverlay(status, '', false);
                        tbody.innerHTML = _jobsErrorTableRow(status, 'Failed to load jobs. Please try again.');
                        _updateJobBulkBar(status);
                        _updateJobsSelectAllMatchingBtn(status);
                    });
            }

            function changeJobPageSize(status, size) {
                const n = parseInt(size, 10);
                if (!TABLE_PAGE_SIZE_OPTIONS.includes(n)) return;
                _jobPageSize = n;
                JOB_STATUSES.forEach(s => {
                    const sel = document.getElementById(`jobPageSize-${s}`);
                    if (sel) sel.value = String(n);
                    currentPages[s] = 1;
                });
                const search = _jobSearchQuery(status) || null;
                loadJobs(status, 1, search);
            }

            // Change page for a specific status
            function changePage(status, direction) {
                const newPage = currentPages[status] + direction;
                if (newPage >= 1) {
                    currentPages[status] = newPage;
                    // Get current search value for this status
                    const searchInput = document.getElementById(`jobSearch-${status}`);
                    const searchJobId = searchInput.value.trim() || null;
                    loadJobs(status, newPage, searchJobId);
                }
            }

            // Search jobs by ID for a specific tab
            function searchJobs(status) {
                const searchInput = document.getElementById(`jobSearch-${status}`);
                const jobId = searchInput.value.trim();
                
                if (jobId === '') {
                    clearSearch(status);
                    return;
                }

                // Reset page to 1 for this status
                currentPages[status] = 1;
                
                // Load jobs for this specific tab
                loadJobs(status, 1, jobId);
            }

            // Clear search for a specific tab
            function clearSearch(status) {
                const searchInput = document.getElementById(`jobSearch-${status}`);
                searchInput.value = '';
                _syncSearchClearBtn(searchInput);
                
                // Reset page to 1 for this status
                currentPages[status] = 1;
                
                // Load jobs for this specific tab
                loadJobs(status, 1);
            }

            // Helper function to format time
            function formatTime(seconds) {
                const hours = Math.floor(seconds / 3600);
                const minutes = Math.floor((seconds % 3600) / 60);
                const secs = Math.floor(seconds % 60);
                return `${hours.toString().padStart(2, '0')}:${minutes.toString().padStart(2, '0')}:${secs.toString().padStart(2, '0')}`;
            }

            // Initialize jobs loading when tab is clicked
            function openTab(event, tabName) {
                var i, tabcontent, tablinks;
                tabcontent = document.getElementsByClassName("tabcontent");
                for (i = 0; i < tabcontent.length; i++) {
                    tabcontent[i].classList.remove("active");
                }
                tablinks = document.getElementsByClassName("tab-button");
                for (i = 0; i < tablinks.length; i++) {
                    tablinks[i].classList.remove("active");
                }
                document.getElementById(tabName).classList.add("active");
                event.currentTarget.classList.add("active");
                
                // Load jobs for the selected tab
                currentStatus = tabName;
                JOB_STATUSES.forEach(s => _updateJobBulkBar(s));
                // Get current search value for this status
                const searchInput = document.getElementById(`jobSearch-${tabName}`);
                const searchJobId = searchInput ? searchInput.value.trim() || null : null;
                loadJobs(tabName, currentPages[tabName], searchJobId);
            }

            // Load initial data when page loads
            document.addEventListener('DOMContentLoaded', function() {
                // Load jobs for all tabs initially
                const statuses = ['SERVED', 'DONE', 'ABORTED', 'PENDING', 'DELETED'];
                statuses.forEach(status => {
                    loadJobs(status, 1);

                    // Add enter key support for each search input
                    const searchInput = document.getElementById(`jobSearch-${status}`);
                    if (searchInput) {
                        _syncSearchClearBtn(searchInput);
                        searchInput.addEventListener('keypress', function(e) {
                            if (e.key === 'Enter') {
                                searchJobs(status);
                            }
                        });
                    }
                });
                _updateWorkersTableTitle();
                const workerSearchInput = document.getElementById('workerSearchInput');
                if (workerSearchInput) {
                    _syncSearchClearBtn(workerSearchInput);
                    workerSearchInput.addEventListener('keypress', function(e) {
                        if (e.key === 'Enter') searchWorkers();
                    });
                }
            });

            let _modalJobId = null;
            let _jobUploads = [];

            function switchModalTab(tabName, btn) {
                document.querySelectorAll('.modal-tab-content').forEach(el => el.classList.remove('active'));
                document.querySelectorAll('.modal-tab-btn').forEach(el => el.classList.remove('active'));
                document.getElementById('modalTab-' + tabName).classList.add('active');
                btn.classList.add('active');
                if (tabName === 'results' && _modalJobId != null) {
                    loadJobUploads(_modalJobId);
                }
            }

            function _formatBytes(n) {
                if (n == null || isNaN(n)) return '—';
                if (n < 1024) return n + ' B';
                if (n < 1024 * 1024) return (n / 1024).toFixed(1) + ' KB';
                return (n / (1024 * 1024)).toFixed(2) + ' MB';
            }

            function _formatUploadTime(ts) {
                if (!ts) return '';
                try {
                    return new Date(ts * 1000).toLocaleString();
                } catch (e) {
                    return String(ts);
                }
            }

            function _resetUploadsTab() {
                _jobUploads = [];
                document.getElementById('uploadsLoading').style.display = 'none';
                document.getElementById('uploadsEmpty').style.display = 'none';
                document.getElementById('uploadsEmpty').innerHTML =
                    '<i class="fas fa-inbox"></i><p>No uploaded results for this job yet.</p>';
                document.getElementById('uploadsPanel').style.display = 'none';
                document.getElementById('uploadVersionSelect').innerHTML = '';
                document.getElementById('uploadMeta').textContent = '';
                document.getElementById('uploadPreviewNotice').style.display = 'none';
                document.getElementById('uploadPreviewArea').innerHTML = '';
                const dl = document.getElementById('uploadDownloadBtn');
                dl.href = '#';
                dl.onclick = function(e) { e.preventDefault(); };
            }

            function loadJobUploads(jobId) {
                _resetUploadsTab();
                document.getElementById('uploadsLoading').style.display = 'block';

                fetch('/job_uploads?job_id=' + encodeURIComponent(jobId))
                    .then(function(resp) {
                        if (!resp.ok) throw new Error('HTTP ' + resp.status);
                        return resp.json();
                    })
                    .then(function(data) {
                        document.getElementById('uploadsLoading').style.display = 'none';
                        _jobUploads = data.uploads || [];
                        if (!_jobUploads.length) {
                            document.getElementById('uploadsEmpty').style.display = 'block';
                            return;
                        }
                        document.getElementById('uploadsPanel').style.display = 'block';
                        const sel = document.getElementById('uploadVersionSelect');
                        sel.innerHTML = '';
                        _jobUploads.forEach(function(u, idx) {
                            const opt = document.createElement('option');
                            opt.value = u.filename;
                            opt.textContent = 'v' + u.version + ' — ' + u.filename + ' (' + _formatBytes(u.size_bytes) + ')';
                            if (idx === 0) opt.selected = true;
                            sel.appendChild(opt);
                        });
                        onUploadVersionChange();
                    })
                    .catch(function(err) {
                        console.error('loadJobUploads failed:', err);
                        document.getElementById('uploadsLoading').style.display = 'none';
                        document.getElementById('uploadsEmpty').style.display = 'block';
                        document.getElementById('uploadsEmpty').innerHTML =
                            '<i class="fas fa-exclamation-triangle"></i><p>Could not load uploads.</p>';
                    });
            }

            function onUploadVersionChange() {
                const jobId = _modalJobId;
                const sel = document.getElementById('uploadVersionSelect');
                const filename = sel.value;
                if (_modalJobId == null || !filename) return;

                const meta = _jobUploads.find(function(u) { return u.filename === filename; }) || {};
                document.getElementById('uploadMeta').textContent =
                    _formatBytes(meta.size_bytes) +
                    (meta.uploaded_at != null ? ' · ' + _formatUploadTime(meta.uploaded_at) : '') +
                    (meta.format ? ' · ' + meta.format : '');

                const dlUrl = '/job_uploads/download?job_id=' + encodeURIComponent(jobId) +
                    '&filename=' + encodeURIComponent(filename);
                const dlBtn = document.getElementById('uploadDownloadBtn');
                dlBtn.href = dlUrl;
                dlBtn.onclick = null;

                const notice = document.getElementById('uploadPreviewNotice');
                const area = document.getElementById('uploadPreviewArea');
                notice.style.display = 'none';
                notice.textContent = '';
                area.innerHTML = '<div class="uploads-loading"><i class="fas fa-spinner fa-spin"></i> Loading preview…</div>';

                fetch('/job_uploads/content?job_id=' + encodeURIComponent(jobId) +
                    '&filename=' + encodeURIComponent(filename))
                    .then(function(resp) {
                        if (!resp.ok) throw new Error('HTTP ' + resp.status);
                        return resp.json();
                    })
                    .then(function(data) {
                        renderUploadPreview(data);
                    })
                    .catch(function(err) {
                        console.error('upload preview failed:', err);
                        area.innerHTML = '<div class="upload-download-only"><i class="fas fa-exclamation-circle"></i>Preview unavailable. Use Download.</div>';
                    });
            }

            function renderUploadPreview(data) {
                const notice = document.getElementById('uploadPreviewNotice');
                const area = document.getElementById('uploadPreviewArea');
                notice.style.display = 'none';
                area.innerHTML = '';

                if (data.too_large) {
                    notice.style.display = 'block';
                    notice.textContent = 'File exceeds 2 MB — preview hidden. Use Download to view on your machine.';
                    area.innerHTML = '<div class="upload-download-only"><i class="fas fa-file-download"></i>File too large for in-browser preview.</div>';
                    return;
                }

                if (!data.viewable) {
                    area.innerHTML = '<div class="upload-download-only"><i class="fas fa-file"></i>Preview not available for this file type. Use Download.</div>';
                    return;
                }

                if (data.format === 'tabular') {
                    area.appendChild(renderTabularPreview(data.content, data.filename));
                } else if (data.format === 'json') {
                    try {
                        const parsed = JSON.parse(data.content);
                        area.appendChild(buildJsonTree(parsed, true));
                    } catch (e) {
                        area.innerHTML = '<pre class="upload-tree">' + escapeHtml(data.content) + '</pre>';
                    }
                } else if (data.format === 'yaml') {
                    area.appendChild(buildYamlTree(data.content));
                } else {
                    area.innerHTML = '<div class="upload-download-only"><i class="fas fa-file"></i>Use Download to view this file.</div>';
                }
            }

            function escapeHtml(text) {
                const d = document.createElement('div');
                d.textContent = text;
                return d.innerHTML;
            }

            function parseDelimitedText(text, filename) {
                const ext = (filename || '').split('.').pop().toLowerCase();
                const delim = (ext === 'tsv' || ext === 'tab') ? '\t' : ',';
                const rows = [];
                let row = [];
                let cell = '';
                let inQuotes = false;
                for (let i = 0; i < text.length; i++) {
                    const ch = text[i];
                    if (inQuotes) {
                        if (ch === '"') {
                            if (text[i + 1] === '"') { cell += '"'; i++; }
                            else inQuotes = false;
                        } else cell += ch;
                    } else if (ch === '"') {
                        inQuotes = true;
                    } else if (ch === delim) {
                        row.push(cell);
                        cell = '';
                    } else if (ch === '\n') {
                        row.push(cell);
                        if (row.length > 1 || row[0] !== '') rows.push(row);
                        row = [];
                        cell = '';
                    } else if (ch !== '\r') {
                        cell += ch;
                    }
                }
                if (cell.length || row.length) {
                    row.push(cell);
                    rows.push(row);
                }
                return rows;
            }

            function renderTabularPreview(content, filename) {
                const wrap = document.createElement('div');
                wrap.className = 'upload-table-wrap';
                const rows = parseDelimitedText(content, filename);
                if (!rows.length) {
                    wrap.innerHTML = '<div class="upload-download-only">Empty table</div>';
                    return wrap;
                }
                const table = document.createElement('table');
                table.className = 'upload-data-table';
                const thead = document.createElement('thead');
                const headRow = document.createElement('tr');
                rows[0].forEach(function(h) {
                    const th = document.createElement('th');
                    th.textContent = h;
                    headRow.appendChild(th);
                });
                thead.appendChild(headRow);
                table.appendChild(thead);
                const tbody = document.createElement('tbody');
                rows.slice(1).forEach(function(r) {
                    const tr = document.createElement('tr');
                    rows[0].forEach(function(_, ci) {
                        const td = document.createElement('td');
                        td.textContent = r[ci] != null ? r[ci] : '';
                        tr.appendChild(td);
                    });
                    tbody.appendChild(tr);
                });
                table.appendChild(tbody);
                wrap.appendChild(table);
                return wrap;
            }

            function buildJsonTree(value, expanded) {
                const root = document.createElement('div');
                root.className = 'upload-tree';
                root.appendChild(jsonTreeNode(null, value, expanded));
                return root;
            }

            function jsonTreeNode(key, value, expanded) {
                const node = document.createElement('div');
                node.className = 'upload-tree-node';

                if (value !== null && typeof value === 'object') {
                    const isArr = Array.isArray(value);
                    const keys = isArr ? value.map(function(_, i) { return i; }) : Object.keys(value);
                    const row = document.createElement('div');
                    row.className = 'upload-tree-row';
                    const toggle = document.createElement('span');
                    toggle.className = 'upload-tree-toggle';
                    toggle.textContent = expanded ? '▼' : '▶';
                    row.appendChild(toggle);
                    const label = document.createElement('span');
                    if (key !== null) {
                        label.innerHTML = '<span class="upload-tree-key">' + escapeHtml(String(key)) + '</span>: ';
                    }
                    label.innerHTML += isArr ? '[ ' + keys.length + ' ]' : '{ ' + keys.length + ' }';
                    row.appendChild(label);
                    node.appendChild(row);

                    const children = document.createElement('div');
                    children.className = 'upload-tree-children';
                    children.style.display = expanded ? 'block' : 'none';
                    keys.forEach(function(k) {
                        children.appendChild(jsonTreeNode(k, value[k], false));
                    });
                    node.appendChild(children);

                    toggle.onclick = function() {
                        const open = children.style.display !== 'none';
                        children.style.display = open ? 'none' : 'block';
                        toggle.textContent = open ? '▶' : '▼';
                    };
                } else {
                    const row = document.createElement('div');
                    row.className = 'upload-tree-row';
                    const spacer = document.createElement('span');
                    spacer.className = 'upload-tree-toggle';
                    spacer.textContent = ' ';
                    row.appendChild(spacer);
                    const valSpan = document.createElement('span');
                    let valHtml = '';
                    if (key !== null) {
                        valHtml += '<span class="upload-tree-key">' + escapeHtml(String(key)) + '</span>: ';
                    }
                    if (value === null) valHtml += '<span class="upload-tree-val-null">null</span>';
                    else if (typeof value === 'string') valHtml += '<span class="upload-tree-val-string">"' + escapeHtml(value) + '"</span>';
                    else if (typeof value === 'number') valHtml += '<span class="upload-tree-val-number">' + value + '</span>';
                    else if (typeof value === 'boolean') valHtml += '<span class="upload-tree-val-bool">' + value + '</span>';
                    else valHtml += escapeHtml(String(value));
                    valSpan.innerHTML = valHtml;
                    row.appendChild(valSpan);
                    node.appendChild(row);
                }
                return node;
            }

            function buildYamlTree(text) {
                const root = document.createElement('div');
                root.className = 'upload-tree';
                const lines = text.split('\n');
                let i = 0;

                function indentOf(line) {
                    const m = line.match(/^(\s*)/);
                    return m ? m[1].length : 0;
                }

                function parseBlock(minIndent) {
                    const block = document.createElement('div');
                    block.className = 'upload-yaml-block';
                    while (i < lines.length) {
                        const line = lines[i];
                        if (!line.trim()) { i++; continue; }
                        const ind = indentOf(line);
                        if (ind < minIndent) break;

                        const nextLine = (i + 1 < lines.length) ? lines[i + 1] : '';
                        const nextInd = nextLine.trim() ? indentOf(nextLine) : -1;
                        const hasChildren = nextInd > ind;

                        if (hasChildren) {
                            const row = document.createElement('div');
                            row.className = 'upload-tree-row';
                            const toggle = document.createElement('span');
                            toggle.className = 'upload-tree-toggle';
                            toggle.textContent = '▼';
                            row.appendChild(toggle);
                            const lbl = document.createElement('span');
                            lbl.className = 'upload-yaml-line';
                            lbl.textContent = line.trim();
                            row.appendChild(lbl);
                            block.appendChild(row);
                            i++;
                            const childWrap = document.createElement('div');
                            childWrap.className = 'upload-tree-children';
                            childWrap.appendChild(parseBlock(nextInd));
                            block.appendChild(childWrap);
                            toggle.onclick = function() {
                                const open = childWrap.style.display !== 'none';
                                childWrap.style.display = open ? 'none' : 'block';
                                toggle.textContent = open ? '▶' : '▼';
                            };
                        } else {
                            const row = document.createElement('div');
                            row.className = 'upload-yaml-line';
                            row.textContent = line.slice(minIndent);
                            block.appendChild(row);
                            i++;
                        }
                    }
                    return block;
                }

                root.appendChild(parseBlock(0));
                return root;
            }

            function getTimelineClass(reason) {
                if (!reason) return '';
                if (reason.startsWith('Job Deleted'))            return 'tl-deleted';
                if (reason.startsWith('Job Restored'))           return 'tl-restored';
                if (reason.startsWith('Parameters Updated'))     return 'tl-params';
                if (reason.startsWith('Manual Status Change'))   return 'tl-status';
                if (reason.startsWith('Job Cleaner'))            return 'tl-cleaner';
                if (reason.includes('requests this job'))        return 'tl-served';
                if (reason.includes('DONE') || reason.includes('completed')) return 'tl-done';
                if (reason.includes('ABORTED') || reason.includes('aborted')) return 'tl-aborted';
                return '';
            }

            function _metricsLevel(ratio) {
                if (ratio >= 0.85) return 'high';
                if (ratio >= 0.60) return 'med';
                return 'low';
            }

            function _num(metrics, key, fallback) {
                const v = metrics[key];
                return (typeof v === 'number' && !Number.isNaN(v)) ? v : fallback;
            }

            function _metricsBar(label, valueText, ratio, icon, dataKey) {
                const row = document.createElement('div');
                row.className = 'metrics-bar-row';
                if (dataKey) row.dataset.metricBar = dataKey;

                const lbl = document.createElement('div');
                lbl.className = 'metrics-bar-label';
                lbl.innerHTML = icon ? `<i class="fas ${icon}"></i> ${label}` : label;

                const trackWrap = document.createElement('div');
                trackWrap.className = 'metrics-bar-track-wrap';

                const track = document.createElement('div');
                track.className = 'metrics-bar-track';

                const fill = document.createElement('div');
                fill.className = 'metrics-bar-fill ' + _metricsLevel(Math.min(1, Math.max(0, ratio)));
                fill.style.width = (Math.min(100, Math.max(0, ratio * 100))).toFixed(1) + '%';

                const val = document.createElement('div');
                val.className = 'metrics-bar-value';
                val.textContent = valueText;

                track.appendChild(fill);
                trackWrap.appendChild(track);
                trackWrap.appendChild(val);
                row.appendChild(lbl);
                row.appendChild(trackWrap);
                return row;
            }

            function _metricsSection(title, icon) {
                const sec = document.createElement('div');
                sec.className = 'metrics-section';
                const h = document.createElement('div');
                h.className = 'metrics-section-title';
                h.innerHTML = icon ? `<i class="fas ${icon}"></i> ${title}` : title;
                sec.appendChild(h);
                return sec;
            }

            function _metricsChip(text, icon) {
                const chip = document.createElement('span');
                chip.className = 'metrics-hardware-chip';
                chip.innerHTML = icon ? `<i class="fas ${icon}"></i> ${text}` : text;
                return chip;
            }

            function _metricsSnapshotValues(metrics) {
                const cpuThreads = Math.max(1, _num(metrics, 'cpu_threads', 1));
                const cpuCores   = _num(metrics, 'cpu_cores', 0);
                const cpuFreq    = _num(metrics, 'cpu_freq_mhz', 0);
                const workerType = metrics.worker_type || 'worker';
                const cpuUtil    = _num(metrics, 'cpu_util', 0);
                const ramUtil    = _num(metrics, 'ram_util', 0);
                const ramTotal   = _num(metrics, 'ram_total', 0);
                const ramAvail   = _num(metrics, 'ram_available', 0);
                const ramUsed    = Math.max(0, ramTotal - ramAvail);
                const diskUtil   = _num(metrics, 'disk_io_util', 0);
                const load1      = _num(metrics, 'load_1min', 0);
                const load5      = _num(metrics, 'load_5min', 0);
                const load15     = _num(metrics, 'load_15min', 0);
                const loadPerCpu = _num(metrics, 'load_per_cpu', 0);
                const idleSlots  = _num(metrics, 'idle_slots', 0);
                return {
                    cpuThreads, cpuCores, cpuFreq, workerType,
                    cpuUtil, ramUtil, ramTotal, ramAvail, ramUsed, diskUtil,
                    load1, load5, load15, loadPerCpu, idleSlots,
                };
            }

            function _updateMetricsBarRow(row, valueText, ratio) {
                if (!row) return;
                const val = row.querySelector('.metrics-bar-value');
                const fill = row.querySelector('.metrics-bar-fill');
                if (val) val.textContent = valueText;
                if (fill) {
                    const r = Math.min(1, Math.max(0, ratio));
                    fill.style.width = (r * 100).toFixed(1) + '%';
                    fill.className = 'metrics-bar-fill ' + _metricsLevel(r);
                }
            }

            function _buildMetricsPanel(metrics) {
                const v = _metricsSnapshotValues(metrics);
                const panel = document.createElement('div');
                panel.className = 'metrics-panel';
                panel.dataset.metricsReady = '1';

                const hw = document.createElement('div');
                hw.className = 'metrics-hardware';
                hw.dataset.metricsHw = '1';
                panel.appendChild(hw);

                const utilSec = _metricsSection('Utilization', 'fa-chart-bar');
                utilSec.appendChild(_metricsBar(
                    'CPU', v.cpuUtil.toFixed(1) + '%', v.cpuUtil / 100, 'fa-microchip', 'cpu',
                ));
                utilSec.appendChild(_metricsBar(
                    'Memory',
                    v.ramUtil.toFixed(1) + '% · ' + v.ramUsed.toFixed(1) + ' / ' + v.ramTotal.toFixed(1) + ' GB',
                    v.ramUtil / 100,
                    'fa-database',
                    'memory',
                ));
                utilSec.appendChild(_metricsBar(
                    'Disk I/O', v.diskUtil.toFixed(1) + '%', v.diskUtil / 100, 'fa-hdd', 'disk',
                ));
                panel.appendChild(utilSec);

                const loadSec = _metricsSection('Load average', 'fa-weight-hanging');
                const loadNote = document.createElement('div');
                loadNote.className = 'metrics-section-note';
                loadNote.dataset.metricsLoadNote = '1';
                loadSec.appendChild(loadNote);
                loadSec.appendChild(_metricsBar('1 min', v.load1.toFixed(2), v.load1 / v.cpuThreads, null, 'load1'));
                loadSec.appendChild(_metricsBar('5 min', v.load5.toFixed(2), v.load5 / v.cpuThreads, null, 'load5'));
                loadSec.appendChild(_metricsBar('15 min', v.load15.toFixed(2), v.load15 / v.cpuThreads, null, 'load15'));
                panel.appendChild(loadSec);

                const stats = document.createElement('div');
                stats.className = 'metrics-stat-grid';

                const idleCard = document.createElement('div');
                idleCard.className = 'metrics-stat-card highlight';
                idleCard.dataset.metricStat = 'idle_slots';
                idleCard.innerHTML =
                    '<div class="metrics-stat-label"><i class="fas fa-hourglass-half"></i> Idle slots</div>' +
                    '<div class="metrics-stat-value"></div>' +
                    '<div class="metrics-stat-hint">estimated free worker capacity</div>';
                stats.appendChild(idleCard);

                const lpcCard = document.createElement('div');
                lpcCard.className = 'metrics-stat-card';
                lpcCard.dataset.metricStat = 'load_per_cpu';
                lpcCard.innerHTML =
                    '<div class="metrics-stat-label"><i class="fas fa-divide"></i> Load / CPU</div>' +
                    '<div class="metrics-stat-value"></div>' +
                    '<div class="metrics-stat-hint">1-min load per thread</div>';
                stats.appendChild(lpcCard);

                const capCard = document.createElement('div');
                capCard.className = 'metrics-stat-card';
                capCard.dataset.metricStat = 'ram_free';
                capCard.innerHTML =
                    '<div class="metrics-stat-label"><i class="fas fa-memory"></i> RAM free</div>' +
                    '<div class="metrics-stat-value"></div>' +
                    '<div class="metrics-stat-hint"></div>';
                stats.appendChild(capCard);

                panel.appendChild(stats);
                _updateMetricsPanel(panel, metrics);
                return panel;
            }

            function _updateMetricsPanel(panel, metrics) {
                const v = _metricsSnapshotValues(metrics);
                const hw = panel.querySelector('[data-metrics-hw]');
                if (hw) {
                    const hwKey = `${v.workerType}|${v.cpuCores}|${v.cpuThreads}|${v.cpuFreq}`;
                    if (hw.dataset.hwKey !== hwKey) {
                        hw.dataset.hwKey = hwKey;
                        hw.innerHTML = '';
                        const badge = document.createElement('span');
                        badge.className = 'metrics-worker-badge';
                        badge.innerHTML = `<i class="fas fa-server"></i> ${_escHtml(v.workerType)}`;
                        hw.appendChild(badge);
                        if (v.cpuCores > 0) hw.appendChild(_metricsChip(`${v.cpuCores} cores`, 'fa-microchip'));
                        if (v.cpuThreads > 0) hw.appendChild(_metricsChip(`${v.cpuThreads} threads`, 'fa-layer-group'));
                        if (v.cpuFreq > 0) hw.appendChild(_metricsChip(`${v.cpuFreq} MHz`, 'fa-tachometer-alt'));
                    }
                }
                const loadNote = panel.querySelector('[data-metrics-load-note]');
                if (loadNote) {
                    loadNote.textContent = 'Relative to ' + v.cpuThreads + ' logical threads';
                }
                _updateMetricsBarRow(panel.querySelector('[data-metric-bar="cpu"]'), v.cpuUtil.toFixed(1) + '%', v.cpuUtil / 100);
                _updateMetricsBarRow(
                    panel.querySelector('[data-metric-bar="memory"]'),
                    v.ramUtil.toFixed(1) + '% · ' + v.ramUsed.toFixed(1) + ' / ' + v.ramTotal.toFixed(1) + ' GB',
                    v.ramUtil / 100,
                );
                _updateMetricsBarRow(panel.querySelector('[data-metric-bar="disk"]'), v.diskUtil.toFixed(1) + '%', v.diskUtil / 100);
                _updateMetricsBarRow(panel.querySelector('[data-metric-bar="load1"]'), v.load1.toFixed(2), v.load1 / v.cpuThreads);
                _updateMetricsBarRow(panel.querySelector('[data-metric-bar="load5"]'), v.load5.toFixed(2), v.load5 / v.cpuThreads);
                _updateMetricsBarRow(panel.querySelector('[data-metric-bar="load15"]'), v.load15.toFixed(2), v.load15 / v.cpuThreads);
                const idleVal = panel.querySelector('[data-metric-stat="idle_slots"] .metrics-stat-value');
                const lpcVal = panel.querySelector('[data-metric-stat="load_per_cpu"] .metrics-stat-value');
                const ramVal = panel.querySelector('[data-metric-stat="ram_free"] .metrics-stat-value');
                const ramHint = panel.querySelector('[data-metric-stat="ram_free"] .metrics-stat-hint');
                if (idleVal) idleVal.textContent = String(v.idleSlots);
                if (lpcVal) lpcVal.textContent = v.loadPerCpu.toFixed(2);
                if (ramVal) ramVal.textContent = v.ramAvail.toFixed(1) + ' GB';
                if (ramHint) ramHint.textContent = 'of ' + v.ramTotal.toFixed(1) + ' GB total';
            }

            function renderSystemMetrics(container, metrics) {
                const existing = container.querySelector('.metrics-panel[data-metrics-ready]');
                if (existing) {
                    _updateMetricsPanel(existing, metrics);
                    return;
                }
                container.innerHTML = '';
                container.appendChild(_buildMetricsPanel(metrics));
            }

            /** Load one job from the API so history/params never break the onclick handler. */
            function openJobDetails(jobId) {
                fetch(`/jobs_paginated?search_job_id=${encodeURIComponent(jobId)}&page=1&per_page=1`)
                    .then(response => {
                        if (!response.ok) {
                            throw new Error(`HTTP ${response.status}`);
                        }
                        return response.json();
                    })
                    .then(data => {
                        const job = data.jobs && data.jobs[0];
                        if (!job || String(job.id) !== String(jobId)) {
                            showNotification('Job not found', 'error');
                            return;
                        }
                        showMessageModalWithRecovery(
                            job.id,
                            job.message,
                            job.parameters,
                            job.system_metrics || {},
                            job.status
                        );
                    })
                    .catch(err => {
                        console.error('openJobDetails failed:', err);
                        showNotification('Could not load job details', 'error');
                    });
            }

            function showMessageModal(jobId, message, parameters, systemMetrics, currentStatus) {
                const modal = document.getElementById("messageModal");
                const jobIdSpan = document.getElementById("jobId");
                const messageTimeline = document.getElementById("messageTimeline");
                const parametersTable = document.getElementById("parametersTable");
                const systemMetricsTable = document.getElementById("systemMetricsTable");
                const statusChangeSection = document.getElementById("statusChangeSection");
                const statusChangeTitle = document.getElementById("statusChangeTitle");
                const deleteSection = document.getElementById("deleteSection");
                const restoreSection = document.getElementById("restoreSection");
                const updateParamsSection = document.getElementById("updateParamsSection");

                _modalJobId = jobId;
                _resetUploadsTab();

                // Set Job ID and status badge
                jobIdSpan.textContent = jobId;
                const badge = document.getElementById("jobStatusBadge");
                badge.textContent = currentStatus;
                badge.className = 'job-status-badge badge-' + currentStatus;

                // Reset to Parameters tab on every open
                switchModalTab('parameters', document.getElementById('tab-btn-parameters'));

                // ---- Actions tab ----
                const hasStatusChange = ['DONE','ABORTED','PENDING'].includes(currentStatus);
                const hasDelete       = currentStatus === 'PENDING';
                const hasRestore      = currentStatus === 'DELETED';
                const actionCount     = (hasStatusChange ? 1 : 0) + (hasDelete ? 1 : 0) + (hasRestore ? 1 : 0);

                statusChangeSection.style.display = hasStatusChange ? 'block' : 'none';
                if (hasStatusChange) {
                    statusChangeTitle.textContent =
                        (currentStatus === 'PENDING') ? 'Change Status to DONE' : 'Change Status to PENDING';
                }
                deleteSection.style.display  = hasDelete  ? 'block' : 'none';
                restoreSection.style.display = hasRestore ? 'block' : 'none';
                document.getElementById("noActionsMsg").style.display = (actionCount === 0) ? 'block' : 'none';
                updateParamsSection.style.display = hasDelete ? 'block' : 'none';

                // Update Actions badge count
                const ab = document.getElementById("actionsTabBadge");
                if (actionCount > 0) { ab.textContent = actionCount; ab.style.display = 'inline-block'; }
                else                 { ab.style.display = 'none'; }

                // Clear old contents
                messageTimeline.innerHTML = "";
                parametersTable.innerHTML = "";
                systemMetricsTable.innerHTML = "";
                document.getElementById("updateParamsFields").innerHTML = "";
                document.getElementById("updateParamsReason").value = "";
                document.getElementById("statusChangeReason").value = "";
                document.getElementById("deleteReason").value = "";
                document.getElementById("restoreReason").value = "";

                // Parameters
                try {
                    let parsedParameters = safeJsonParse(parameters, {});
                    console.log('Parsed parameters:', parsedParameters);

                    if (parsedParameters && typeof parsedParameters === "object" && !Array.isArray(parsedParameters) && Object.keys(parsedParameters).length > 0) {
                        const table = document.createElement("table");
                        table.className = "modal-kv-table";
                        table.innerHTML = `<tr><th>Parameter</th><th>Value</th></tr>`;
                        for (let key in parsedParameters) {
                            const val = parsedParameters[key];
                            const display = (typeof val === 'object') ? JSON.stringify(val) : String(val);
                            const row = document.createElement("tr");
                            row.innerHTML = `<td><strong>${key}</strong></td><td style="font-family:monospace">${display}</td>`;
                            table.appendChild(row);
                        }
                        parametersTable.appendChild(table);
                    } else {
                        parametersTable.innerHTML = '<p style="color:#adb5bd; text-align:center; padding:20px 0;">No parameters</p>';
                    }

                    if (currentStatus === 'PENDING' && parsedParameters && typeof parsedParameters === "object" && !Array.isArray(parsedParameters)) {
                        const fieldsContainer = document.getElementById("updateParamsFields");
                        for (let key in parsedParameters) {
                            const val = parsedParameters[key];
                            const isArray = Array.isArray(val);
                            const isObject = !isArray && typeof val === 'object' && val !== null;
                            const isNumber = typeof val === 'number';

                            const fieldDiv = document.createElement("div");
                            fieldDiv.style.cssText = "display: flex; align-items: flex-start; gap: 8px; margin-bottom: 8px;";

                            const label = document.createElement("label");
                            label.textContent = key;
                            label.style.cssText = "min-width: 130px; font-weight: bold; padding-top: 6px; word-break: break-all;";

                            let input;
                            if (isArray || isObject) {
                                input = document.createElement("textarea");
                                input.rows = 3;
                                input.value = JSON.stringify(val, null, 2);
                                input.style.cssText = "flex: 1; padding: 6px; border: 1px solid #ffe066; border-radius: 4px; font-family: monospace; font-size: 0.85em; resize: vertical;";
                                input.setAttribute("data-type", isArray ? "array" : "object");
                            } else if (isNumber) {
                                input = document.createElement("input");
                                input.type = "number";
                                input.value = val;
                                input.step = Number.isInteger(val) ? "1" : "any";
                                input.style.cssText = "flex: 1; padding: 6px; border: 1px solid #ffe066; border-radius: 4px;";
                                input.setAttribute("data-type", "number");
                            } else {
                                input = document.createElement("input");
                                input.type = "text";
                                input.value = val;
                                input.style.cssText = "flex: 1; padding: 6px; border: 1px solid #ffe066; border-radius: 4px;";
                                input.setAttribute("data-type", "string");
                            }
                            input.setAttribute("data-key", key);

                            fieldDiv.appendChild(label);
                            fieldDiv.appendChild(input);
                            fieldsContainer.appendChild(fieldDiv);
                        }
                    }
                } catch (e) {
                    console.error('Error parsing parameters:', e);
                    parametersTable.innerHTML = '<p style="color:#adb5bd; text-align:center; padding:20px 0;">Could not load parameters</p>';
                }

                // Metrics
                try {
                    let parsedSystemMetrics = safeJsonParse(systemMetrics, {});
                    console.log('Parsed system_metrics:', parsedSystemMetrics);

                    if (parsedSystemMetrics && typeof parsedSystemMetrics === "object" && !Array.isArray(parsedSystemMetrics) && Object.keys(parsedSystemMetrics).length > 0) {
                        renderSystemMetrics(systemMetricsTable, parsedSystemMetrics);
                    } else {
                        systemMetricsTable.innerHTML = '<p style="color:#adb5bd; text-align:center; padding:20px 0;">No system metrics recorded — collected when a worker claims this job.</p>';
                    }
                } catch (e) {
                    console.error('Error parsing system_metrics:', e);
                    systemMetricsTable.innerHTML = '<p style="color:#adb5bd; text-align:center; padding:20px 0;">Could not load metrics</p>';
                }

                // History (isolated — bad message data must not break other tabs)
                try {
                    console.log('Raw message:', message);
                    renderJobHistory(messageTimeline, message);
                } catch (e) {
                    console.error('Error rendering history:', e);
                    messageTimeline.innerHTML =
                        '<div class="timeline-empty"><i class="fas fa-exclamation-triangle"></i>Could not load history for this job.</div>';
                }

                modal.style.display = "block";
            }

            function toggleParams()   { switchModalTab('parameters', document.getElementById('tab-btn-parameters')); }
            function toggleMetrics() { switchModalTab('metrics',    document.getElementById('tab-btn-metrics'));    }

            function closeMessageModal() {
                document.getElementById("messageModal").style.display = "none";
            }

            function changeJobStatus() {
                const jobId = document.getElementById("jobId").textContent;
                const status = document.getElementById("jobStatusBadge").textContent;
                const action = status === 'PENDING' ? 'to_done' : 'to_pending';
                const reason = document.getElementById("statusChangeReason")?.value || '';
                requestJobAction(action, 'job', status, jobId, reason);
            }

            function deleteJob() {
                const jobId = document.getElementById("jobId").textContent;
                const reason = document.getElementById("deleteReason")?.value || '';
                requestJobAction('delete', 'job', 'PENDING', jobId, reason);
            }

            function restoreJob() {
                const jobId = document.getElementById("jobId").textContent;
                const reason = document.getElementById("restoreReason")?.value || '';
                requestJobAction('restore', 'job', 'DELETED', jobId, reason);
            }

            function updateJobParameters() {
                const jobId = document.getElementById("jobId").textContent;
                const reason = document.getElementById("updateParamsReason").value;
                const fields = document.querySelectorAll("#updateParamsFields [data-key]");

                const updates = {};
                let hasError = false;

                fields.forEach(field => {
                    if (hasError) return;
                    const key = field.getAttribute("data-key");
                    const type = field.getAttribute("data-type");
                    const raw = field.value.trim();

                    if (type === 'array' || type === 'object') {
                        try {
                            updates[key] = JSON.parse(raw);
                        } catch (e) {
                            showNotification(`Invalid JSON for parameter "${key}": ${e.message}`, "error");
                            hasError = true;
                        }
                    } else if (type === 'number') {
                        const num = Number(raw);
                        if (isNaN(num)) {
                            showNotification(`Invalid number for parameter "${key}"`, "error");
                            hasError = true;
                        } else {
                            updates[key] = num;
                        }
                    } else {
                        updates[key] = raw;
                    }
                });

                if (hasError) return;

                if (Object.keys(updates).length === 0) {
                    showNotification("No parameters to update.", "info");
                    return;
                }

                const btn = document.getElementById("updateParamsBtn");
                const originalText = btn.textContent;
                btn.textContent = "Saving...";
                btn.disabled = true;

                fetch('/update_job_parameters', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ job_id: parseInt(jobId), updates: updates, reason: reason })
                })
                .then(response => response.json())
                .then(data => {
                    if (data.success) {
                        showNotification("Parameters updated successfully.", "success");
                        document.getElementById("updateParamsReason").value = "";
                        setTimeout(() => { closeMessageModal(); location.reload(); }, 1500);
                    } else {
                        showNotification("Error: " + (data.error || "Failed to update parameters"), "error");
                    }
                })
                .catch(error => {
                    showNotification("Error updating parameters: " + error.message, "error");
                })
                .finally(() => {
                    btn.textContent = originalText;
                    btn.disabled = false;
                });
            }

            // ====================== SETTINGS MODAL ======================

            function openSettingsModal() {
                const errEl   = document.getElementById("settingsError");
                const lockEl  = document.getElementById("settingsLockBanner");
                const saveBtn = document.getElementById("settingsSaveBtn");
                const idleIn  = document.getElementById("settingsIdleTimeout");
                const abortIn = document.getElementById("settingsAbortedTimeout");

                errEl.textContent = "";

                // Always clear PIN fields and their error — prevent browser autofill residue
                document.getElementById("currentPin").value     = "";
                document.getElementById("newPin").value         = "";
                document.getElementById("pinUpdateError").textContent = "";

                // Load current values (idle_timeout stored as seconds, dropdown shows minutes)
                fetch('/server_config')
                    .then(r => r.json())
                    .then(data => {
                        if (data.idle_timeout !== undefined) {
                            const minutes = Math.round(data.idle_timeout / 60);
                            idleIn.value = Math.min(Math.max(minutes, 1), 5); // clamp 1–5
                        }
                        if (data.aborted_job_reset_timeout !== undefined) {
                            const abortMins = Math.round(data.aborted_job_reset_timeout / 60);
                            const abortOpts = [1, 5, 10, 20, 30, 60];
                            // Pick the closest option
                            const closest = abortOpts.reduce((a, b) => Math.abs(b - abortMins) < Math.abs(a - abortMins) ? b : a);
                            abortIn.value = closest;
                        }
                    }).catch(() => {});

                // Lock the form if any jobs are currently SERVED
                fetch('/jobs_paginated?status=SERVED&per_page=1&page=1')
                    .then(r => r.json())
                    .then(data => {
                        const locked = data.total_count > 0;
                        lockEl.style.display  = locked ? 'flex' : 'none';
                        saveBtn.disabled      = locked;
                        saveBtn.style.opacity = locked ? '0.5' : '1';
                        saveBtn.style.cursor  = locked ? 'not-allowed' : 'pointer';
                        idleIn.disabled  = locked;
                        abortIn.disabled = locked;
                    }).catch(() => {});

                document.getElementById("settingsModal").style.display = "block";
            }

            function closeSettingsModal() {
                document.getElementById("settingsModal").style.display = "none";
            }

            function saveSettings() {
                const idleMinutes   = parseInt(document.getElementById("settingsIdleTimeout").value);
                const idleSeconds   = idleMinutes * 60;
                const abortMinutes  = parseInt(document.getElementById("settingsAbortedTimeout").value);
                const abortSeconds  = abortMinutes * 60;
                const errEl         = document.getElementById("settingsError");
                errEl.textContent   = "";

                fetch('/update_server_config', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ idle_timeout: idleSeconds, aborted_job_reset_timeout: abortSeconds })
                })
                .then(r => r.json())
                .then(data => {
                    if (data.success) {
                        showNotification(`Settings saved — idle: ${idleMinutes} min, aborted reset: ${abortMinutes} min. Takes effect on next cleaner cycle.`, "success");
                        closeSettingsModal();
                    } else {
                        errEl.textContent = data.error || "Failed to save settings.";
                    }
                })
                .catch(e => { errEl.textContent = "Network error: " + e.message; });
            }

            // ====================== SETUP OVERLAY ======================

            function openSetupOverlay() {
                document.getElementById("setupOverlay").style.display = "block";
                document.getElementById("setupOverlayCloseBtn").style.display = "block";
                document.getElementById("setupError").textContent = "";
                document.getElementById("replaceExistingJobs").checked = false;
                // Show replace-warning row (jobs already exist when user manually opens this)
                document.getElementById("replaceRow").style.display = "block";
                // Reset to Form tab and clear JSON panel state
                switchAddJobsTab("form");
                document.getElementById("jsonParamInput").value = "";
                document.getElementById("jsonValidationMsg").textContent = "";
                document.getElementById("jsonValidationMsg").className = "json-validation-msg";
                document.getElementById("jsonPreviewSection").style.display = "none";
            }

            function closeSetupOverlay() {
                document.getElementById("setupOverlay").style.display = "none";
            }

            function addParameterRow(name, values) {
                const container = document.getElementById("parameterRows");
                const row = document.createElement("div");
                row.className = "param-row";
                const nameVal = name || "";
                const valVal  = values || "";
                row.innerHTML =
                    `<input type="text" placeholder="parameter name" class="param-name" value="${nameVal}">` +
                    `<textarea class="param-values" rows="1" placeholder='[1, 2, 4]'>${valVal}</textarea>` +
                    `<button class="param-del" onclick="removeParameterRow(this)" title="Remove"><i class="fas fa-times"></i></button>`;
                row.querySelector('.param-values').addEventListener('input', updateJobCountPreview);
                row.querySelector('.param-name').addEventListener('input', updateJobCountPreview);
                container.appendChild(row);
                updateJobCountPreview();
            }

            function removeParameterRow(btn) {
                btn.closest(".param-row").remove();
                updateJobCountPreview();
            }

            function updateJobCountPreview() {
                const rows = document.querySelectorAll("#parameterRows .param-row");
                const preview = document.getElementById("jobCountPreview");
                if (rows.length === 0) { preview.textContent = ""; return; }
                let total = 1;
                const parts = [];
                let allValid = true;
                rows.forEach(row => {
                    const raw = row.querySelector(".param-values").value.trim();
                    if (!raw) { allValid = false; return; }
                    try {
                        const arr = JSON.parse(raw);
                        if (!Array.isArray(arr) || arr.length === 0) { allValid = false; return; }
                        total *= arr.length;
                        parts.push(arr.length);
                    } catch(e) { allValid = false; }
                });
                if (allValid && parts.length === rows.length) {
                    preview.textContent = parts.join(" \u00d7 ") + " = " + total + " job" + (total !== 1 ? "s" : "");
                    preview.className = "";
                } else {
                    preview.textContent = "Fix JSON arrays to preview count";
                    preview.className = "error";
                }
            }

            function submitCreateJobs() {
                const errEl = document.getElementById("setupError");
                errEl.textContent = "";

                let parameters = {};

                if (_addJobsActiveTab === "json") {
                    // JSON mode
                    const res = _parseJsonParams();
                    if (!res.ok) { errEl.textContent = res.err; return; }
                    parameters = res.params;
                } else {
                    // Form mode
                    const rows = document.querySelectorAll("#parameterRows .param-row");
                    if (rows.length === 0) { errEl.textContent = "Add at least one parameter."; return; }
                    let hasError = false;
                    rows.forEach(row => {
                        if (hasError) return;
                        const name = row.querySelector(".param-name").value.trim();
                        const raw  = row.querySelector(".param-values").value.trim();
                        if (!name) { errEl.textContent = "Every parameter must have a name."; hasError = true; return; }
                        if (name in parameters) { errEl.textContent = `Duplicate parameter name: "${name}".`; hasError = true; return; }
                        try {
                            const arr = JSON.parse(raw);
                            if (!Array.isArray(arr) || arr.length === 0) {
                                errEl.textContent = `"${name}": value must be a non-empty JSON array.`;
                                hasError = true; return;
                            }
                            parameters[name] = arr;
                        } catch(e) {
                            errEl.textContent = `"${name}": invalid JSON — ${e.message}`;
                            hasError = true;
                        }
                    });
                    if (hasError) return;
                }

                const replace = document.getElementById("replaceExistingJobs").checked;

                const btn = document.getElementById("createJobsBtn");
                const orig = btn.innerHTML;
                btn.innerHTML = '<i class="fas fa-spinner fa-spin"></i> ' + (replace ? 'Replacing...' : 'Adding...');
                btn.disabled = true;

                fetch('/create_jobs', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ parameters: parameters, replace: replace })
                })
                .then(r => r.json())
                .then(data => {
                    if (data.success) {
                        showNotification(data.total_jobs + " jobs " + (data.action === "Created" ? "created (replaced all)" : "appended") + " successfully!", "success");
                        closeSetupOverlay();
                        setTimeout(() => location.reload(), 1500);
                    } else {
                        errEl.textContent = data.error || "Failed to create jobs.";
                    }
                })
                .catch(e => { errEl.textContent = "Network error: " + e.message; })
                .finally(() => { btn.innerHTML = orig; btn.disabled = false; });
            }

            // ── Add Jobs tab switcher ──────────────────────────────────────────
            let _addJobsActiveTab = "form";

            function switchAddJobsTab(tab) {
                _addJobsActiveTab = tab;
                document.getElementById("addjobs-tab-form").classList.toggle("active", tab === "form");
                document.getElementById("addjobs-tab-json").classList.toggle("active", tab === "json");
                document.getElementById("addjobs-panel-form").style.display = tab === "form" ? "" : "none";
                document.getElementById("addjobs-panel-json").style.display = tab === "json" ? "" : "none";
                document.getElementById("setupError").textContent = "";
            }

            // ── JSON tab helpers ───────────────────────────────────────────────

            function _parseJsonParams() {
                const raw = document.getElementById("jsonParamInput").value.trim();
                if (!raw) return { ok: false, err: "JSON input is empty." };
                let obj;
                try { obj = JSON.parse(raw); } catch (e) { return { ok: false, err: "Invalid JSON: " + e.message }; }
                if (typeof obj !== "object" || Array.isArray(obj) || obj === null)
                    return { ok: false, err: "JSON must be an object, e.g. { \"lr\": [0.01, 0.1] }." };
                for (const [k, v] of Object.entries(obj)) {
                    if (!Array.isArray(v) || v.length === 0)
                        return { ok: false, err: `"${k}": value must be a non-empty array.` };
                }
                if (Object.keys(obj).length === 0)
                    return { ok: false, err: "Object must have at least one parameter." };
                return { ok: true, params: obj };
            }

            function _cartesian(params) {
                const keys   = Object.keys(params);
                const values = keys.map(k => params[k]);
                let result   = [{}];
                for (let i = 0; i < keys.length; i++) {
                    const next = [];
                    for (const combo of result) {
                        for (const val of values[i]) {
                            next.push(Object.assign({}, combo, { [keys[i]]: val }));
                        }
                    }
                    result = next;
                }
                return result;
            }

            function formatJsonInput() {
                const el = document.getElementById("jsonParamInput");
                const msg = document.getElementById("jsonValidationMsg");
                try {
                    const obj = JSON.parse(el.value);
                    el.value = JSON.stringify(obj, null, 2);
                    msg.textContent = "";
                    msg.className = "json-validation-msg";
                    _renderJsonPreview();
                } catch (e) {
                    msg.textContent = "Cannot format — invalid JSON: " + e.message;
                    msg.className = "json-validation-msg json-validation-error";
                }
            }

            function validateJsonInput() {
                const msg = document.getElementById("jsonValidationMsg");
                const res = _parseJsonParams();
                if (res.ok) {
                    const total = _cartesian(res.params).length;
                    msg.textContent = `Valid — will generate ${total} job${total !== 1 ? "s" : ""}.`;
                    msg.className = "json-validation-msg json-validation-ok";
                    _renderJsonPreview();
                } else {
                    msg.textContent = res.err;
                    msg.className = "json-validation-msg json-validation-error";
                    document.getElementById("jsonPreviewSection").style.display = "none";
                }
            }

            function _renderJsonPreview() {
                const res = _parseJsonParams();
                const section = document.getElementById("jsonPreviewSection");
                if (!res.ok) { section.style.display = "none"; return; }

                const jobs    = _cartesian(res.params);
                const keys    = Object.keys(res.params);
                const preview = jobs.slice(0, 5);
                const total   = jobs.length;

                document.getElementById("jsonJobCountBadge").textContent =
                    total + " job" + (total !== 1 ? "s" : "") + " will be created";
                document.getElementById("jsonPreviewShowing").textContent =
                    Math.min(5, total) + " of " + total;

                // Header
                const thead = document.getElementById("jsonPreviewHead");
                thead.innerHTML = "<tr>" + keys.map(k => `<th>${k}</th>`).join("") + "</tr>";

                // Rows
                const tbody = document.getElementById("jsonPreviewBody");
                tbody.innerHTML = preview.map(job =>
                    "<tr>" + keys.map(k => `<td>${JSON.stringify(job[k])}</td>`).join("") + "</tr>"
                ).join("");

                section.style.display = "";
            }

            // Live preview as the user types
            document.getElementById("jsonParamInput") && document.getElementById("jsonParamInput")
                .addEventListener("input", () => {
                    const msg = document.getElementById("jsonValidationMsg");
                    msg.textContent = "";
                    msg.className = "json-validation-msg";
                    _renderJsonPreview();
                });

            // ====================== END SETUP OVERLAY ======================

            function showNotification(message, type = "info") {
                // Remove existing notifications
                const existingNotifications = document.querySelectorAll('.notification');
                existingNotifications.forEach(notification => notification.remove());
                
                // Create notification element
                const notification = document.createElement('div');
                notification.className = `notification notification-${type}`;
                notification.innerHTML = `
                    <div class="notification-content">
                        <span class="notification-message">${message}</span>
                        <button class="notification-close" onclick="this.parentElement.parentElement.remove()">×</button>
                    </div>
                `;
                
                // Add to page
                document.body.appendChild(notification);
                
                // Auto-remove after 5 seconds for success/info, 8 seconds for warnings/errors
                const autoRemoveTime = (type === "success" || type === "info") ? 5000 : 8000;
                setTimeout(() => {
                    if (notification.parentElement) {
                        notification.remove();
                    }
                }, autoRemoveTime);
            }

            window.onclick = function(event) {
                const modal = document.getElementById("messageModal");
                if (event.target === modal) {
                    modal.style.display = "none";
                }
            };

            /**
             * Sort a table by column.
             * iconEl  : the <span class="sort-icon"> that was clicked  (passed as `this`)
             * colIndex: zero-based column index
             * dataType: "number" reads data-value attribute (raw numeric); "string" reads innerText
             */
            function sortTable(iconEl, colIndex, dataType) {
                // Walk up from the icon span to the nearest <table>
                let el = iconEl;
                while (el && el.tagName !== 'TABLE') el = el.parentElement;
                if (!el) return;

                const tbody = el.querySelector('tbody');
                const rows  = Array.from(tbody.querySelectorAll('tr'));
                if (rows.length === 0) return;

                // Toggle asc/desc on the clicked icon; reset others in the same table
                const asc = iconEl.getAttribute('data-asc') !== 'true';
                el.querySelectorAll('.sort-icon').forEach(ic => {
                    ic.textContent = '↕';
                    ic.removeAttribute('data-asc');
                });
                iconEl.setAttribute('data-asc', asc);
                iconEl.textContent = asc ? '↑' : '↓';

                rows.sort((a, b) => {
                    const ca = a.children[colIndex];
                    const cb = b.children[colIndex];
                    if (!ca || !cb) return 0;

                    if (dataType === 'number') {
                        const va = parseFloat(ca.getAttribute('data-value') ?? ca.innerText) || 0;
                        const vb = parseFloat(cb.getAttribute('data-value') ?? cb.innerText) || 0;
                        return asc ? va - vb : vb - va;
                    }
                    const ta = ca.innerText.trim();
                    const tb = cb.innerText.trim();
                    return asc ? ta.localeCompare(tb) : tb.localeCompare(ta);
                });

                rows.forEach(r => tbody.appendChild(r));
            }

            // Enhanced error handling and validation
            function validateJobData(job) {
                if (!job || typeof job !== 'object') {
                    console.error('Invalid job data:', job);
                    return false;
                }
                
                if (!job.id || !job.status) {
                    console.error('Job missing required fields:', job);
                    return false;
                }
                
                return true;
            }



            // Comprehensive error recovery for message modal
            function showMessageModalWithRecovery(jobId, message, parameters, systemMetrics, currentStatus) {
                try {
                    showMessageModal(jobId, message, parameters, systemMetrics, currentStatus);
                } catch (error) {
                    console.error('Error in showMessageModal, attempting recovery:', error);
                    
                    const modal = document.getElementById("messageModal");
                    const jobIdSpan = document.getElementById("jobId");
                    const messageTimeline = document.getElementById("messageTimeline");
                    const parametersTable = document.getElementById("parametersTable");
                    const systemMetricsTable = document.getElementById("systemMetricsTable");
                    const badge = document.getElementById("jobStatusBadge");
                    
                    if (modal && jobIdSpan) {
                        jobIdSpan.textContent = jobId;
                        if (badge) {
                            badge.textContent = currentStatus || '';
                            badge.className = 'job-status-badge badge-' + (currentStatus || 'PENDING');
                        }
                        switchModalTab('parameters', document.getElementById('tab-btn-parameters'));

                        if (parametersTable) {
                            parametersTable.innerHTML = '<p style="color:#adb5bd; text-align:center; padding:20px 0;">Could not load parameters</p>';
                        }
                        if (systemMetricsTable) {
                            systemMetricsTable.innerHTML = '<p style="color:#adb5bd; text-align:center; padding:20px 0;">Could not load metrics</p>';
                        }
                        if (messageTimeline) {
                            try {
                                renderJobHistory(messageTimeline, message);
                            } catch (historyErr) {
                                messageTimeline.innerHTML =
                                    '<div class="timeline-empty"><i class="fas fa-exclamation-triangle"></i>Could not load history for this job.</div>';
                            }
                        }
                        modal.style.display = "block";
                    } else {
                        alert(`Error displaying job ${jobId}. Please check console for details.`);
                    }
                }
            }

let chart;
            function updateChart() {
                let interval = document.getElementById("timeInterval").value;
                let machine = document.getElementById("machineFilter").value;
                fetch(`/job_stats?interval=` + interval + `&machine=` + machine)
                    .then(response => response.json())
                    .then(data => {
                        if (chart) { chart.destroy(); }
                        
                        // Convert timestamps to local time for chart labels
                        let formattedLabels = data.labels;
                        if (data.timestamps) {
                            formattedLabels = data.labels.map(timestamp => {
                                if (typeof timestamp === 'number') {
                                    const date = new Date(timestamp * 1000);
                                    if (interval === 'minutely') {
                                        return date.toLocaleTimeString([], {hour: '2-digit', minute: '2-digit'});
                                    } else if (interval === 'hourly') {
                                        return date.toLocaleTimeString([], {hour: '2-digit', minute: '2-digit'});
                                    } else {
                                        return date.toLocaleDateString([], {month: 'short', day: 'numeric'});
                                    }
                                }
                                return timestamp;
                            });
                        }
                        
                        let ctx = document.getElementById("jobChart").getContext("2d");
                        chart = new Chart(ctx, {
                            type: "bar",
                            data: {
                                labels: formattedLabels,
                                datasets: [{
                                    label: "Jobs Completed",
                                    data: data.values,
                                    backgroundColor: "#3498db",
                                    borderColor: "#2980b9",
                                    borderWidth: 1
                                }]
                            },
                            options: {
                                responsive: true,
                                maintainAspectRatio: false,
                                scales: { 
                                    y: { beginAtZero: true } 
                                },
                                plugins: {
                                    legend: {
                                        display: true,
                                        position: 'top'
                                    }
                                }
                            }
                        });
                        // Update the total jobs count in the HTML element with id "totalJobs"
                        document.getElementById("totalJobs").innerText = data.total_jobs;
                    });
            }
            updateChart();

// Function to convert Unix timestamp to local time
            function formatTimestamp(timestamp) {
                if (!timestamp || timestamp === 'N/A') return 'N/A';
                const date = new Date(timestamp * 1000); // Convert Unix timestamp to milliseconds
                return date.toLocaleString(); // Use browser's local timezone
            }
            
            // Function to convert all timestamps in the table to local time
            function convertTimestampsToLocal() {
                const timestampCells = document.querySelectorAll('[data-timestamp]');
                timestampCells.forEach(cell => {
                    const timestamp = cell.getAttribute('data-timestamp');
                    if (timestamp && timestamp !== 'N/A') {
                        cell.textContent = formatTimestamp(parseFloat(timestamp));
                    }
                });
            }
            
            // Function to handle watermark visibility
            function handleTableWatermarks() {
                const tables = document.querySelectorAll('.myTable');
                tables.forEach(table => {
                    const tableWrapper = table.closest('.table-wrapper');
                    if (tableWrapper?.querySelector('.jobs-table-overlay')) {
                        return;
                    }
                    const tbody = table.querySelector('tbody');
                    
                    if (tbody) {
                        const visibleRows = tbody.querySelectorAll('tr:not(.dtr-hidden)');
                        
                        // Check if watermark exists, if not create it
                        let watermark = tableWrapper.querySelector('.table-watermark');
                        if (!watermark) {
                            watermark = document.createElement('div');
                            watermark.className = 'table-watermark';
                            watermark.innerHTML = '<i class="fas fa-database"></i><div class="watermark-text">No Data Available</div>';
                            tableWrapper.appendChild(watermark);
                        }
                        
                        if (visibleRows.length === 0) {
                            watermark.style.display = 'block';
                        } else {
                            watermark.style.display = 'none';
                        }
                    }
                });
            }
            
           $(document).ready(function () {
                // Convert timestamps to local time
                convertTimestampsToLocal();
                
                // Handle watermark visibility
                handleTableWatermarks();

                // Always seed the parameter form with one blank row
                addParameterRow();

                // Show setup overlay automatically when no jobs exist yet
                fetch('/jobs_paginated?per_page=1&page=1')
                    .then(r => r.json())
                    .then(data => {
                        if (data.total_count === 0) {
                            document.getElementById("setupOverlay").style.display = "block";
                            // No close button — user must create jobs first
                            document.getElementById("setupOverlayCloseBtn").style.display = "none";
                            // No existing jobs — hide the replace warning
                            document.getElementById("replaceRow").style.display = "none";
                            document.getElementById("replaceExistingJobs").checked = false;
                        }
                    })
                    .catch(() => {});

                // Load traffic stats on page open, then refresh every 30 s
                loadTrafficStats();
                setInterval(loadTrafficStats, 30000);
            });

            function formatBytes(bytes) {
                if (bytes === 0) return '0 B';
                const units = ['B', 'KB', 'MB', 'GB', 'TB'];
                const i = Math.floor(Math.log(bytes) / Math.log(1024));
                const val = bytes / Math.pow(1024, i);
                return val.toFixed(i === 0 ? 0 : 1) + ' ' + units[i];
            }

            function trafficRow(labelIn, labelOut, bytesIn, bytesOut) {
                return `<div style="display:flex; gap:6px;">
                    <span data-tooltip="${labelIn}" style="flex:1; background:#e8f8ef; color:#1a7a3c; border-radius:5px; padding:4px 8px; font-size:0.75rem; text-align:center; cursor:default;">
                        <i class="fas fa-arrow-down"></i> ${formatBytes(bytesIn)}
                    </span>
                    <span data-tooltip="${labelOut}" style="flex:1; background:#e8f0ff; color:#2e4db5; border-radius:5px; padding:4px 8px; font-size:0.75rem; text-align:center; cursor:default;">
                        <i class="fas fa-arrow-up"></i> ${formatBytes(bytesOut)}
                    </span>
                </div>`;
            }

            // ── Auth helpers ─────────────────────────────────────────────────────────

            function refreshDashboard() {
                window.location.reload();
            }

            function logout() {
                fetch('/auth/logout', { method: 'POST' })
                    .then(() => { window.location.href = '/auth'; })
                    .catch(() => { window.location.href = '/auth'; });
            }

            function updatePin() {
                const currentEl = document.getElementById('currentPin');
                const newEl     = document.getElementById('newPin');
                const errEl     = document.getElementById('pinUpdateError');
                const current   = currentEl.value.trim();
                const newPin    = newEl.value.trim();
                errEl.textContent = '';

                if (!/^\d{6}$/.test(current)) {
                    errEl.textContent = 'Current PIN must be exactly 6 digits.';
                    currentEl.focus(); return;
                }
                if (!/^\d{6}$/.test(newPin)) {
                    errEl.textContent = 'New PIN must be exactly 6 digits.';
                    newEl.focus(); return;
                }
                if (current === newPin) {
                    errEl.textContent = 'New PIN must be different from the current PIN.';
                    newEl.focus(); return;
                }

                // Use _origFetch so the global 401 interceptor doesn't interfere
                _origFetch('/update_pin', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({current_pin: current, new_pin: newPin})
                })
                .then(r => r.json().then(d => ({ok: r.ok, data: d})))
                .then(({ok, data}) => {
                    if (ok && data.success) {
                        showNotification(
                            data.message || 'PIN updated. Sign in again with your new PIN.',
                            'success'
                        );
                        window.location.href = '/auth';
                    } else {
                        errEl.textContent = data.error || 'Failed to update PIN.';
                        if (data.error && data.error.includes('Current')) currentEl.focus();
                        else newEl.focus();
                    }
                })
                .catch(e => { errEl.textContent = 'Network error: ' + e.message; });
            }

            // Redirect to /auth on any 401 response from API calls
            const _origFetch = window.fetch;
            window.fetch = function(...args) {
                return _origFetch(...args).then(resp => {
                    if (resp.status === 401) {
                        window.location.href = '/auth';
                    }
                    return resp;
                });
            };

            // ─────────────────────────────────────────────────────────────────────────

            // ── Global JS tooltip engine ────────────────────────────────────────────
            // Creates a single #js-tooltip div appended to <body> so it is never
            // clipped by overflow:hidden/auto on any ancestor (sidebar, modals, etc.)
            (function () {
                const tip = document.createElement('div');
                tip.id = 'js-tooltip';
                document.body.appendChild(tip);

                const MARGIN = 12; // px gap between cursor and tooltip box

                function show(el, e) {
                    const text = el.getAttribute('data-tooltip');
                    if (!text) return;
                    tip.textContent = text;
                    tip.classList.add('visible');
                    position(e);
                }

                function position(e) {
                    const tw = tip.offsetWidth;
                    const th = tip.offsetHeight;
                    let x = e.clientX - tw / 2;
                    let y = e.clientY - th - MARGIN;

                    // Keep inside viewport
                    x = Math.max(6, Math.min(x, window.innerWidth  - tw - 6));
                    y = Math.max(6, Math.min(y, window.innerHeight - th - 6));
                    // If tooltip would go above viewport, show it below cursor instead
                    if (y < 6) y = e.clientY + MARGIN;

                    tip.style.left = x + 'px';
                    tip.style.top  = y + 'px';
                }

                function hide() { tip.classList.remove('visible'); }

                document.addEventListener('mouseover', e => {
                    const el = e.target.closest('[data-tooltip]');
                    if (el) show(el, e);
                });
                document.addEventListener('mousemove', e => {
                    if (tip.classList.contains('visible')) position(e);
                });
                document.addEventListener('mouseout', e => {
                    if (!e.relatedTarget || !e.relatedTarget.closest('[data-tooltip]')) hide();
                });
            })();
            // ───────────────────────────────────────────────────────────────────────

            function loadTrafficStats() {
                fetch('/traffic_stats')
                    .then(r => r.json())
                    .then(data => {
                        const el = document.getElementById('trafficStats');
                        if (!el) return;

                        const totalIn  = data.server_in  + data.dashboard_in;
                        const totalOut = data.server_out + data.dashboard_out;

                        const sectionLabel = (text) =>
                            `<div style="font-size:0.78rem; font-weight:700; color:#495057; margin-top:6px; margin-bottom:3px;">${text}</div>`;

                        el.innerHTML =
                            sectionLabel('Job Server') +
                            trafficRow(
                                'Bytes received by the job server from workers and clients',
                                'Bytes sent out by the job server to workers and clients',
                                data.server_in, data.server_out
                            ) +
                            sectionLabel('Dashboard') +
                            trafficRow(
                                'Bytes received by the dashboard from your browser',
                                'Bytes sent out by the dashboard to your browser',
                                data.dashboard_in, data.dashboard_out
                            ) +
                            `<div style="border-top:1px solid #e3e7f0; margin:8px 0 4px;"></div>` +
                            sectionLabel('Total') +
                            trafficRow(
                                'Total bytes received across both services',
                                'Total bytes sent out across both services',
                                totalIn, totalOut
                            );
                    })
                    .catch(() => {
                        const el = document.getElementById('trafficStats');
                        if (el) el.innerHTML = '<div style="color:#adb5bd; font-size:0.78rem; text-align:center; padding:8px 0;">Unavailable</div>';
                    });
            }