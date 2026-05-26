function openModal() {
                document.getElementById("statsModal").style.display = "block";
            }
            function closeModal() {
                document.getElementById("statsModal").style.display = "none";
            }

            // ── Main nav: Jobs vs Workers ─────────────────────────────────────
            let _workerSubtab = 'active';
            let _workerFiltersData = null;
            let _workerDetailCache = null;

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
                    refreshWorkersPage();
                } else {
                    workersView.classList.remove('active');
                    jobsView.classList.add('active');
                    tabWorkers.classList.remove('active');
                    tabJobs.classList.add('active');
                }
            }

            function switchWorkerSubtab(which) {
                _workerSubtab = which;
                document.getElementById('workerSubtabActive').classList.toggle('active', which === 'active');
                document.getElementById('workerSubtabDisabled').classList.toggle('active', which === 'disabled');
                const toolbar = document.getElementById('workersActiveToolbar');
                if (toolbar) toolbar.style.display = which === 'active' ? 'flex' : 'none';
                loadWorkerFilters().then(() => loadWorkersPageTable());
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

            function _workerStateBadge(w) {
                if ((w.lifecycle_status || 'active') === 'disabled') {
                    return '<span class="worker-badge worker-badge--disabled">disabled</span>';
                }
                if (w.pending) {
                    return `<span class="worker-badge worker-badge--pending">queued ${w.desired_state}</span>`;
                }
                const ds = w.desired_state || 'run';
                if (ds !== 'run') {
                    return `<span class="worker-badge worker-badge--applied">${ds}</span>`;
                }
                const rs = w.reported_status || 'idle';
                const cls = rs === 'busy' ? 'worker-badge--busy' : 'worker-badge--idle';
                return `<span class="worker-badge ${cls}">${rs}</span>`;
            }

            function loadWorkerFilters() {
                const lifecycle = _workerSubtab === 'disabled' ? 'disabled' : 'active';
                return fetch('/workers/filters?lifecycle=' + lifecycle)
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
                        _populateWorkerInstanceDropdown();
                        if ((data.instances_by_host[hostSel.value] || []).includes(prevInst)) {
                            instSel.value = prevInst;
                        }
                        _populateWorkerSlotDropdown();
                        if (slotSel.querySelector(`option[value="${prevSlot}"]`)) {
                            slotSel.value = prevSlot;
                        }
                    })
                    .catch(() => {});
            }

            function _populateWorkerInstanceDropdown() {
                const hostSel = document.getElementById('workerFilterHost');
                const instSel = document.getElementById('workerFilterInstance');
                if (!instSel || !_workerFiltersData) return;
                const host = hostSel.value;
                instSel.innerHTML = '<option value="">All instances</option>';
                instSel.disabled = !host;
                if (host) {
                    (_workerFiltersData.instances_by_host[host] || []).forEach(inst => {
                        instSel.innerHTML += `<option value="${_escHtml(inst)}">${_escHtml(inst)}</option>`;
                    });
                }
            }

            function _populateWorkerSlotDropdown() {
                const hostSel = document.getElementById('workerFilterHost');
                const instSel = document.getElementById('workerFilterInstance');
                const slotSel = document.getElementById('workerFilterSlot');
                if (!slotSel || !_workerFiltersData) return;
                const host = hostSel.value;
                const inst = instSel.value;
                slotSel.innerHTML = '<option value="">All slots</option>';
                slotSel.disabled = !(host && inst);
                if (host && inst) {
                    const key = host + '|' + inst;
                    (_workerFiltersData.slots_by_host_instance[key] || []).forEach(sl => {
                        slotSel.innerHTML += `<option value="${sl}">${sl}</option>`;
                    });
                }
            }

            function onWorkerFilterChange() {
                const hostSel = document.getElementById('workerFilterHost');
                const instSel = document.getElementById('workerFilterInstance');
                if (!hostSel.value) {
                    instSel.value = '';
                }
                _populateWorkerInstanceDropdown();
                if (!instSel.value) {
                    document.getElementById('workerFilterSlot').value = '';
                }
                _populateWorkerSlotDropdown();
                loadWorkersPageTable();
            }

            function _workerFilterParams() {
                const p = new URLSearchParams();
                p.set('lifecycle', _workerSubtab === 'disabled' ? 'disabled' : 'active');
                const host = document.getElementById('workerFilterHost')?.value;
                const inst = document.getElementById('workerFilterInstance')?.value;
                const slot = document.getElementById('workerFilterSlot')?.value;
                if (host) p.set('host', host);
                if (inst) p.set('instance', inst);
                if (slot !== '') p.set('slot', slot);
                return p.toString();
            }

            function loadWorkersPageTable() {
                const tbody = document.getElementById('workersPageBody');
                if (!tbody) return;
                tbody.innerHTML = '<tr><td colspan="8" class="workers-loading">Loading workers…</td></tr>';
                fetch('/workers/list?' + _workerFilterParams())
                    .then(r => r.json())
                    .then(data => {
                        const workers = data.workers || [];
                        if (!workers.length) {
                            const msg = _workerSubtab === 'disabled'
                                ? 'No disabled workers.'
                                : 'No active workers. Start workers with <code>jd_worker_cli</code> — they appear after the first poll (~3 min).';
                            tbody.innerHTML = `<tr><td colspan="8" class="workers-empty">${msg}</td></tr>`;
                            return;
                        }
                        tbody.innerHTML = workers.map(w => {
                            const wid = w.worker_id || '';
                            const jobCell = w.current_job_id ? `#${w.current_job_id}` : '—';
                            const lastPoll = w.last_poll_at_fmt || '—';
                            const isActive = _workerSubtab === 'active';
                            let actions = `<button type="button" class="workers-btn-sm" onclick="openWorkerDetail('${_escHtml(wid)}')">Details</button>`;
                            if (isActive) {
                                actions += `
                                    <button type="button" class="workers-btn-sm workers-btn--drain" onclick="workerCommand('drain','worker','${_escHtml(wid)}')">Drain</button>
                                    <button type="button" class="workers-btn-sm workers-btn--stop" onclick="workerCommand('stop','worker','${_escHtml(wid)}')">Stop</button>
                                    <button type="button" class="workers-btn-sm workers-btn--run" onclick="workerCommand('run','worker','${_escHtml(wid)}')">Resume</button>
                                    ${w.pending ? `<button type="button" class="workers-btn-sm workers-btn--cancel" onclick="cancelWorkerCommand('worker','${_escHtml(wid)}')">Cancel</button>` : ''}`;
                            }
                            return `<tr>
                                <td><code class="workers-id">${_escHtml(wid)}</code></td>
                                <td>${_escHtml(w.host || '—')}</td>
                                <td><code>${_escHtml(w.instance || '—')}</code></td>
                                <td>${w.slot != null ? w.slot : '—'}</td>
                                <td>${_workerStateBadge(w)}</td>
                                <td>${jobCell}</td>
                                <td>${_escHtml(lastPoll)}</td>
                                <td class="workers-row-actions">${actions}</td>
                            </tr>`;
                        }).join('');
                    })
                    .catch(err => {
                        tbody.innerHTML = `<tr><td colspan="8" class="workers-error">Failed to load: ${_escHtml(err.message)}</td></tr>`;
                    });
            }

            function refreshWorkersPage() {
                loadWorkerSummary();
                loadWorkerFilters().then(() => loadWorkersPageTable());
            }

            function workerCommand(action, scope, target) {
                const labels = { run: 'resume', drain: 'drain', stop: 'stop' };
                const label = labels[action] || action;
                const scopeLabel = scope === 'all' ? 'all active workers'
                    : scope === 'host' ? `host ${target}`
                    : scope === 'instance' ? `instance ${target}`
                    : `worker ${target}`;
                if (!confirm(`Queue ${label} for ${scopeLabel}? Applies on next worker poll (~3 min).`)) return;
                fetch('/workers/command', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ action, scope, target: target || null }),
                })
                    .then(r => r.json().then(d => ({ ok: r.ok, data: d })))
                    .then(({ ok, data }) => {
                        if (ok && data.success) {
                            showNotification(`Queued ${label} for ${data.affected} worker(s).`, 'success');
                            refreshWorkersPage();
                        } else {
                            showNotification(data.error || 'Command failed.', 'error');
                        }
                    })
                    .catch(e => showNotification('Network error: ' + e.message, 'error'));
            }

            function cancelWorkerCommand(scope, target) {
                const scopeLabel = scope === 'all' ? 'all active workers'
                    : scope === 'host' ? `host ${target}` : `worker ${target}`;
                if (!confirm(`Cancel pending commands for ${scopeLabel}?`)) return;
                fetch('/workers/cancel', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ scope, target: target || null }),
                })
                    .then(r => r.json().then(d => ({ ok: r.ok, data: d })))
                    .then(({ ok, data }) => {
                        if (ok && data.success) {
                            showNotification(`Reverted ${data.reverted} pending command(s).`, 'success');
                            refreshWorkersPage();
                        } else {
                            showNotification(data.error || 'Cancel failed.', 'error');
                        }
                    })
                    .catch(e => showNotification('Network error: ' + e.message, 'error'));
            }

            function switchWorkerModalTab(tab, btn) {
                ['info', 'history', 'metrics'].forEach(t => {
                    document.getElementById('modalTab-worker-' + t).classList.toggle('active', t === tab);
                });
                document.querySelectorAll('#workerDetailModal .modal-tab-btn').forEach(b => b.classList.remove('active'));
                if (btn) btn.classList.add('active');
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
                            ['Worker version', w.jd_worker_version || '—'],
                            ['First poll', w.first_poll_at_fmt || '—'],
                            ['Last poll', w.last_poll_at_fmt || '—'],
                            ['Disabled at', w.disabled_at_fmt || '—'],
                        ];
                        info.innerHTML = '<table class="modal-kv-table"><tr><th>Field</th><th>Value</th></tr>' +
                            rows.map(([k, v]) => `<tr><td><strong>${k}</strong></td><td>${_escHtml(String(v))}</td></tr>`).join('') +
                            '</table>';

                        const timeline = document.getElementById('workerHistoryTimeline');
                        renderWorkerHistory(timeline, w.history || []);

                        const metricsEl = document.getElementById('workerMetricsTable');
                        if (w.system_metrics && Object.keys(w.system_metrics).length) {
                            renderSystemMetrics(metricsEl, w.system_metrics);
                        } else {
                            metricsEl.innerHTML = '<p style="color:#adb5bd;text-align:center;padding:20px 0;">No metrics recorded yet.</p>';
                        }
                        _populateWorkerHistoryMetricsSelect(w.history || []);

                        switchWorkerModalTab('info', document.getElementById('tab-btn-worker-info'));
                        document.getElementById('workerDetailModal').style.display = 'block';
                    })
                    .catch(e => showNotification('Could not load worker: ' + e.message, 'error'));
            }

            function closeWorkerDetailModal() {
                document.getElementById('workerDetailModal').style.display = 'none';
            }

            function renderWorkerHistory(timelineEl, entries) {
                timelineEl.innerHTML = '';
                const list = Array.isArray(entries) ? entries.slice() : [];
                list.sort((a, b) => (floatOrZero(b.timestamp) - floatOrZero(a.timestamp)));
                if (!list.length) {
                    timelineEl.innerHTML = '<div class="timeline-empty"><i class="fas fa-history"></i>No history yet.</div>';
                    return;
                }
                list.forEach(entry => {
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
                    let extra = '';
                    if (entry.metrics && Object.keys(entry.metrics).length) {
                        extra = ' <span class="worker-history-metrics-tag">(metrics captured)</span>';
                    }
                    item.innerHTML =
                        `<div class="tl-msg">${formatMessageForDisplay(entry.reason)}${extra}</div>` +
                        `<div class="tl-time">${tsLabel}</div>`;
                    timelineEl.appendChild(item);
                });
            }

            function floatOrZero(v) {
                const n = parseFloat(v);
                return Number.isFinite(n) ? n : 0;
            }

            function _populateWorkerHistoryMetricsSelect(history) {
                const sel = document.getElementById('workerHistoryMetricsSelect');
                const section = document.getElementById('workerHistoryMetricsSection');
                const panel = document.getElementById('workerHistoryMetricsPanel');
                const withMetrics = (history || []).filter(e => e.metrics && Object.keys(e.metrics).length);
                if (!withMetrics.length) {
                    section.style.display = 'none';
                    panel.innerHTML = '';
                    return;
                }
                section.style.display = 'block';
                sel.innerHTML = withMetrics.map((e, i) => {
                    const ts = e.timestamp ? new Date(e.timestamp * 1000).toLocaleString() : 'unknown time';
                    return `<option value="${i}">${_escHtml((e.event || 'event') + ' — ' + ts)}</option>`;
                }).join('');
                onWorkerHistoryMetricsSelect();
            }

            function onWorkerHistoryMetricsSelect() {
                const sel = document.getElementById('workerHistoryMetricsSelect');
                const panel = document.getElementById('workerHistoryMetricsPanel');
                if (!_workerDetailCache || !sel) return;
                const withMetrics = (_workerDetailCache.history || []).filter(
                    e => e.metrics && Object.keys(e.metrics).length
                );
                const idx = parseInt(sel.value, 10);
                if (!withMetrics[idx]) {
                    panel.innerHTML = '';
                    return;
                }
                renderSystemMetrics(panel, withMetrics[idx].metrics);
            }

            document.addEventListener('DOMContentLoaded', function() {
                loadWorkerSummary();
                setInterval(loadWorkerSummary, 30000);
            });

// Pagination and Search Variables
            let currentPages = {
                'SERVED': 1,
                'DONE': 1,
                'ABORTED': 1,
                'PENDING': 1
            };
            let currentSearchJobId = null;
            let currentStatus = 'SERVED';

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
                const loadingRow = `<tr><td colspan="6" class="loading-message"><i class="fas fa-spinner fa-spin"></i> Loading jobs...</td></tr>`;
                tbody.innerHTML = loadingRow;

                const params = new URLSearchParams({
                    page: page,
                    per_page: 50,
                    status: status
                });

                if (searchJobId) {
                    params.append('search_job_id', searchJobId);
                }

                fetch(`/jobs_paginated?${params}`)
                    .then(response => response.json())
                    .then(data => {
                        if (data.error) {
                            tbody.innerHTML = `
                                <tr>
                                    <td colspan="6" class="empty-state error-state">
                                        <div class="empty-icon">
                                            <i class="fas fa-exclamation-triangle"></i>
                                        </div>
                                        <div class="empty-text">Error Loading Jobs</div>
                                        <div class="empty-subtext">${data.error}</div>
                                    </td>
                                </tr>
                            `;
                            return;
                        }

                        // Update pagination info
                        document.getElementById(`page-info-${status}`).textContent = `Page ${data.current_page} of ${data.total_pages}`;
                        document.getElementById(`prev-${status}`).disabled = data.current_page <= 1;
                        document.getElementById(`next-${status}`).disabled = data.current_page >= data.total_pages;

                        // Update pagination info for this status
                        document.getElementById(`paginationInfo-${status}`).textContent = 
                            `Showing ${((data.current_page - 1) * data.per_page) + 1}-${Math.min(data.current_page * data.per_page, data.total_count)} of ${data.total_count} jobs`;

                        // Render jobs
                        if (data.jobs.length === 0) {
                            tbody.innerHTML = `
                                <tr>
                                    <td colspan="6" class="empty-state">
                                        <div class="empty-icon">
                                            <i class="fas fa-database"></i>
                                        </div>
                                        <div class="empty-text">No jobs found</div>
                                        <div class="empty-subtext">No ${status.toLowerCase()} jobs available</div>
                                    </td>
                                </tr>
                            `;
                            return;
                        }

                        let html = '';
                        data.jobs.forEach(job => {
                            const machine        = job.machine || '';
                            const reqTs          = job.request_timestamp    || 0;
                            const compTs         = job.completion_timestamp || 0;
                            const reqTime        = reqTs  ? new Date(reqTs  * 1000).toLocaleString() : '';
                            const compTime       = compTs ? new Date(compTs * 1000).toLocaleString() : '';
                            const durationSec    = job.required_time || 0;
                            const durationFmt    = durationSec ? formatTime(durationSec) : '';

                            html += `
                                <tr>
                                    <td data-value="${job.id}" style="font-weight:bold;">${job.id}</td>
                                    <td>${machine}</td>
                                    <td data-value="${reqTs}">${reqTime}</td>
                                    <td data-value="${compTs}">${compTime}</td>
                                    <td data-value="${durationSec}">${durationFmt}</td>
                                    <td>
                                        <button class="view-details-btn" onclick="openJobDetails(${job.id})" title="View Details">
                                            <i class="fas fa-eye"></i>
                                        </button>
                                    </td>
                                </tr>
                            `;
                        });
                        tbody.innerHTML = html;
                    })
                    .catch(error => {
                        console.error('Error loading jobs:', error);
                        tbody.innerHTML = `
                            <tr>
                                <td colspan="6" class="empty-state error-state">
                                    <div class="empty-icon">
                                        <i class="fas fa-exclamation-triangle"></i>
                                    </div>
                                    <div class="empty-text">Error Loading Jobs</div>
                                    <div class="empty-subtext">Failed to load jobs. Please try again.</div>
                                </td>
                            </tr>
                        `;
                    });
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
                        searchInput.addEventListener('keypress', function(e) {
                            if (e.key === 'Enter') {
                                searchJobs(status);
                            }
                        });
                    }
                });
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

            function _metricsBar(label, valueText, ratio, icon) {
                const row = document.createElement('div');
                row.className = 'metrics-bar-row';

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

            function renderSystemMetrics(container, metrics) {
                container.innerHTML = '';
                const panel = document.createElement('div');
                panel.className = 'metrics-panel';

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

                // Hardware header
                const hw = document.createElement('div');
                hw.className = 'metrics-hardware';
                const badge = document.createElement('span');
                badge.className = 'metrics-worker-badge';
                badge.innerHTML = `<i class="fas fa-server"></i> ${workerType}`;
                hw.appendChild(badge);
                if (cpuCores > 0)   hw.appendChild(_metricsChip(`${cpuCores} cores`, 'fa-microchip'));
                if (cpuThreads > 0) hw.appendChild(_metricsChip(`${cpuThreads} threads`, 'fa-layer-group'));
                if (cpuFreq > 0)    hw.appendChild(_metricsChip(`${cpuFreq} MHz`, 'fa-tachometer-alt'));
                panel.appendChild(hw);

                // Utilization
                const utilSec = _metricsSection('Utilization', 'fa-chart-bar');
                utilSec.appendChild(_metricsBar('CPU', cpuUtil.toFixed(1) + '%', cpuUtil / 100, 'fa-microchip'));
                utilSec.appendChild(_metricsBar(
                    'Memory',
                    ramUtil.toFixed(1) + '% · ' + ramUsed.toFixed(1) + ' / ' + ramTotal.toFixed(1) + ' GB',
                    ramUtil / 100,
                    'fa-database'
                ));
                utilSec.appendChild(_metricsBar('Disk I/O', diskUtil.toFixed(1) + '%', diskUtil / 100, 'fa-hdd'));
                panel.appendChild(utilSec);

                // Load averages
                const loadSec = _metricsSection('Load average', 'fa-weight-hanging');
                const loadNote = document.createElement('div');
                loadNote.className = 'metrics-section-note';
                loadNote.textContent = 'Relative to ' + cpuThreads + ' logical threads';
                loadSec.appendChild(loadNote);
                loadSec.appendChild(_metricsBar('1 min', load1.toFixed(2), load1 / cpuThreads, null));
                loadSec.appendChild(_metricsBar('5 min', load5.toFixed(2), load5 / cpuThreads, null));
                loadSec.appendChild(_metricsBar('15 min', load15.toFixed(2), load15 / cpuThreads, null));
                panel.appendChild(loadSec);

                // Summary cards
                const stats = document.createElement('div');
                stats.className = 'metrics-stat-grid';

                const idleCard = document.createElement('div');
                idleCard.className = 'metrics-stat-card highlight';
                idleCard.innerHTML =
                    '<div class="metrics-stat-label"><i class="fas fa-hourglass-half"></i> Idle slots</div>' +
                    '<div class="metrics-stat-value">' + idleSlots + '</div>' +
                    '<div class="metrics-stat-hint">estimated free worker capacity</div>';
                stats.appendChild(idleCard);

                const lpcCard = document.createElement('div');
                lpcCard.className = 'metrics-stat-card';
                lpcCard.innerHTML =
                    '<div class="metrics-stat-label"><i class="fas fa-divide"></i> Load / CPU</div>' +
                    '<div class="metrics-stat-value">' + loadPerCpu.toFixed(2) + '</div>' +
                    '<div class="metrics-stat-hint">1-min load per thread</div>';
                stats.appendChild(lpcCard);

                const capCard = document.createElement('div');
                capCard.className = 'metrics-stat-card';
                capCard.innerHTML =
                    '<div class="metrics-stat-label"><i class="fas fa-memory"></i> RAM free</div>' +
                    '<div class="metrics-stat-value">' + ramAvail.toFixed(1) + ' GB</div>' +
                    '<div class="metrics-stat-hint">of ' + ramTotal.toFixed(1) + ' GB total</div>';
                stats.appendChild(capCard);

                panel.appendChild(stats);
                container.appendChild(panel);
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
                const reason = document.getElementById("statusChangeReason").value;
                const title = document.getElementById("statusChangeTitle").textContent;
                
                // Determine new status from title
                let newStatus = "";
                if (title.includes("to PENDING")) {
                    newStatus = "PENDING";
                } else if (title.includes("to DONE")) {
                    newStatus = "DONE";
                }
                
                if (!newStatus) {
                    showNotification("Unable to determine target status", "error");
                    return;
                }
                
                // Show loading state
                const changeBtn = document.getElementById("changeStatusBtn");
                const originalText = changeBtn.textContent;
                changeBtn.textContent = "Changing...";
                changeBtn.disabled = true;
                
                fetch('/change_job_status', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                    },
                    body: JSON.stringify({
                        job_id: parseInt(jobId),
                        new_status: newStatus,
                        reason: reason
                    })
                })
                .then(response => response.json())
                .then(data => {
                    if (data.success) {
                        showNotification("Status changed successfully!", "success");
                        // Reset form
                        document.getElementById("statusChangeReason").value = "";
                        // Close modal after a short delay
                        setTimeout(() => {
                            closeMessageModal();
                            location.reload();
                        }, 1500);
                    } else {
                        showNotification("Error: " + (data.error || "Failed to change status"), "error");
                    }
                })
                .catch(error => {
                    console.error('Error:', error);
                    showNotification("Error changing status: " + error.message, "error");
                })
                .finally(() => {
                    // Reset button state
                    changeBtn.textContent = originalText;
                    changeBtn.disabled = false;
                });
            }

            function deleteJob() {
                const jobId = document.getElementById("jobId").textContent;
                const reason = document.getElementById("deleteReason").value;

                const btn = document.getElementById("deleteJobBtn");
                const originalText = btn.textContent;
                btn.textContent = "Deleting...";
                btn.disabled = true;

                fetch('/delete_job', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ job_id: parseInt(jobId), reason: reason })
                })
                .then(response => response.json())
                .then(data => {
                    if (data.success) {
                        showNotification("Job deleted successfully.", "success");
                        document.getElementById("deleteReason").value = "";
                        setTimeout(() => { closeMessageModal(); location.reload(); }, 1500);
                    } else {
                        showNotification("Error: " + (data.error || "Failed to delete job"), "error");
                    }
                })
                .catch(error => {
                    showNotification("Error deleting job: " + error.message, "error");
                })
                .finally(() => {
                    btn.textContent = originalText;
                    btn.disabled = false;
                });
            }

            function restoreJob() {
                const jobId = document.getElementById("jobId").textContent;
                const reason = document.getElementById("restoreReason").value;

                const btn = document.getElementById("restoreJobBtn");
                const originalText = btn.textContent;
                btn.textContent = "Restoring...";
                btn.disabled = true;

                fetch('/restore_job', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ job_id: parseInt(jobId), reason: reason })
                })
                .then(response => response.json())
                .then(data => {
                    if (data.success) {
                        showNotification("Job restored to PENDING successfully.", "success");
                        document.getElementById("restoreReason").value = "";
                        setTimeout(() => { closeMessageModal(); location.reload(); }, 1500);
                    } else {
                        showNotification("Error: " + (data.error || "Failed to restore job"), "error");
                    }
                })
                .catch(error => {
                    showNotification("Error restoring job: " + error.message, "error");
                })
                .finally(() => {
                    btn.textContent = originalText;
                    btn.disabled = false;
                });
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
                        showNotification('PIN updated successfully.', 'success');
                        currentEl.value = '';
                        newEl.value     = '';
                        errEl.textContent = '';
                        closeSettingsModal();
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