function openModal() {
                document.getElementById("statsModal").style.display = "block";
            }
            function closeModal() {
                document.getElementById("statsModal").style.display = "none";
            }

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
                    
                    if (typeof text !== 'string') {
                        text = safeStringify(text);
                        console.log('After safeStringify:', text);
                    }
                    
                    if (!text || text === 'null' || text === 'undefined') {
                        console.log('Empty/null/undefined, returning empty string');
                        return '';
                    }
                    
                    // Handle essential characters for onclick + preserve newlines
                    const result = text
                        .replace(/"/g, '&quot;')          // Double quote
                        .replace(/'/g, '&#39;')           // Single quote
                        .replace(/&/g, '&amp;')           // Ampersand
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

            function safeJsonParse(jsonString, fallback = null) {
                try {
                    console.log('safeJsonParse input:', jsonString);
                    console.log('safeJsonParse input type:', typeof jsonString);
                    
                    // First decode HTML entities
                    const decoded = decodeFromHtmlAttribute(jsonString);
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
                    console.error('Original string:', jsonString);
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

                            const messageJson      = encodeForHtmlAttribute(job.message);
                            const parametersJson   = encodeForHtmlAttribute(job.parameters);
                            const systemMetricsJson = encodeForHtmlAttribute(job.system_metrics || {});

                            html += `
                                <tr>
                                    <td data-value="${job.id}" style="font-weight:bold;">${job.id}</td>
                                    <td>${machine}</td>
                                    <td data-value="${reqTs}">${reqTime}</td>
                                    <td data-value="${compTs}">${compTime}</td>
                                    <td data-value="${durationSec}">${durationFmt}</td>
                                    <td>
                                        <button class="view-details-btn" onclick="showMessageModalWithRecovery(${job.id}, '${messageJson}', '${parametersJson}', '${systemMetricsJson}', '${job.status}')" title="View Details">
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

            function switchModalTab(tabName, btn) {
                document.querySelectorAll('.modal-tab-content').forEach(el => el.classList.remove('active'));
                document.querySelectorAll('.modal-tab-btn').forEach(el => el.classList.remove('active'));
                document.getElementById('modalTab-' + tabName).classList.add('active');
                btn.classList.add('active');
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

                // Set Job ID and status badge
                jobIdSpan.textContent = jobId;
                const badge = document.getElementById("jobStatusBadge");
                badge.textContent = currentStatus;
                badge.className = 'job-status-badge badge-' + currentStatus;

                // Reset to History tab on every open
                switchModalTab('history', document.getElementById('tab-btn-history'));

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

                try {
                    // Debug: Log the raw data
                    console.log('Raw message:', message);
                    console.log('Raw parameters:', parameters);
                    console.log('Raw system_metrics:', systemMetrics);
                    
                    // Parse parameters using safe JSON parsing
                    let parsedParameters = safeJsonParse(parameters, {});
                    console.log('Parsed parameters:', parsedParameters);

                    // Show parameters table (always visible in Parameters tab)
                    if (parsedParameters && typeof parsedParameters === "object" && Object.keys(parsedParameters).length > 0) {
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

                    // Populate editable fields for PENDING jobs
                    if (currentStatus === 'PENDING' && parsedParameters && typeof parsedParameters === "object") {
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

                    // Parse system_metrics using safe JSON parsing
                    let parsedSystemMetrics = safeJsonParse(systemMetrics, {});
                    console.log('Parsed system_metrics:', parsedSystemMetrics);

                    // Show system_metrics table (always visible in Metrics tab)
                    if (parsedSystemMetrics && typeof parsedSystemMetrics === "object" && Object.keys(parsedSystemMetrics).length > 0) {
                        const table = document.createElement("table");
                        table.className = "modal-kv-table";
                        table.innerHTML = `<tr><th>Metric</th><th>Value</th></tr>`;
                        for (let key in parsedSystemMetrics) {
                            const row = document.createElement("tr");
                            let value = parsedSystemMetrics[key];
                            if (typeof value === 'number') {
                                if (key.includes('util') || key.includes('percent')) value = value.toFixed(1) + '%';
                                else if (key.includes('ram') && (key.includes('available') || key.includes('total'))) value = value.toFixed(2) + ' GB';
                                else if (key.includes('freq')) value = value + ' MHz';
                                else value = value.toFixed(2);
                            }
                            row.innerHTML = `<td><strong>${key}</strong></td><td>${value}</td>`;
                            table.appendChild(row);
                        }
                        systemMetricsTable.appendChild(table);
                    } else {
                        systemMetricsTable.innerHTML = '<p style="color:#adb5bd; text-align:center; padding:20px 0;">No system metrics recorded — collected when a worker claims this job.</p>';
                    }

                    // Parse message using safe JSON parsing
                    let parsedMessage = safeJsonParse(message, []);
                    console.log('Parsed message:', parsedMessage);

                    // Show job history (newest first)
                    const reversedMessage = JSON.parse(JSON.stringify(parsedMessage)).reverse();

                    if (reversedMessage.length === 0) {
                        messageTimeline.innerHTML =
                            '<div class="timeline-empty"><i class="fas fa-history"></i>No history yet — audit entries appear here when actions are taken on this job.</div>';
                    } else {
                        reversedMessage.forEach(entry => {
                            const item = document.createElement("div");
                            item.className = "timeline-item " + getTimelineClass(entry.reason || "");

                            const ts = new Date(entry.timestamp * 1000).toLocaleString("en-US", {
                                weekday: "short", year: "numeric", month: "short",
                                day: "numeric", hour: "2-digit", minute: "2-digit",
                                second: "2-digit", hour12: true
                            });

                            item.innerHTML =
                                `<div class="tl-msg">${formatMessageForDisplay(entry.reason)}</div>` +
                                `<div class="tl-time">${ts}</div>`;
                            messageTimeline.appendChild(item);
                        });
                    }

                } catch (e) {
                    console.error('Error parsing message/parameters:', e);
                    messageTimeline.innerHTML = "<p>Invalid message format</p>";
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
                const rows = document.querySelectorAll("#parameterRows .param-row");
                const errEl = document.getElementById("setupError");
                errEl.textContent = "";

                if (rows.length === 0) {
                    errEl.textContent = "Add at least one parameter.";
                    return;
                }

                const parameters = {};
                let hasError = false;

                rows.forEach(row => {
                    if (hasError) return;
                    const name = row.querySelector(".param-name").value.trim();
                    const raw  = row.querySelector(".param-values").value.trim();
                    if (!name) {
                        errEl.textContent = "Every parameter must have a name.";
                        hasError = true; return;
                    }
                    if (name in parameters) {
                        errEl.textContent = `Duplicate parameter name: "${name}".`;
                        hasError = true; return;
                    }
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
                    
                    // Fallback: show basic information without parsing
                    const modal = document.getElementById("messageModal");
                    const jobIdSpan = document.getElementById("jobId");
                    const messageTimeline = document.getElementById("messageTimeline");
                    
                    if (modal && jobIdSpan && messageTimeline) {
                        jobIdSpan.textContent = jobId;
                        messageTimeline.innerHTML = `
                            <div class="timeline-item">
                                <div class="message">Job ID: ${jobId}</div>
                                <div class="timestamp">Status: ${currentStatus}</div>
                            </div>
                            <div class="timeline-item">
                                <div class="message">⚠️ Error displaying job details</div>
                                <div class="timestamp">Please check console for details</div>
                            </div>
                        `;
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

            function loadTrafficStats() {
                fetch('/traffic_stats')
                    .then(r => r.json())
                    .then(data => {
                        const el = document.getElementById('trafficStats');
                        if (!el) return;
                        el.innerHTML = `
                            <div style="display:flex; flex-direction:column; gap:7px;">
                                <div style="display:flex; justify-content:space-between; align-items:center;">
                                    <span style="font-size:0.78rem; font-weight:700; color:#495057;">Job Server</span>
                                    <span></span>
                                </div>
                                <div style="display:flex; gap:6px;">
                                    <span style="flex:1; background:#e8f8ef; color:#1a7a3c; border-radius:5px; padding:4px 8px; font-size:0.75rem; text-align:center;">
                                        <i class="fas fa-arrow-down"></i> ${formatBytes(data.server_in)}
                                    </span>
                                    <span style="flex:1; background:#e8f0ff; color:#2e4db5; border-radius:5px; padding:4px 8px; font-size:0.75rem; text-align:center;">
                                        <i class="fas fa-arrow-up"></i> ${formatBytes(data.server_out)}
                                    </span>
                                </div>
                                <div style="display:flex; justify-content:space-between; align-items:center; margin-top:4px;">
                                    <span style="font-size:0.78rem; font-weight:700; color:#495057;">Dashboard</span>
                                    <span></span>
                                </div>
                                <div style="display:flex; gap:6px;">
                                    <span style="flex:1; background:#e8f8ef; color:#1a7a3c; border-radius:5px; padding:4px 8px; font-size:0.75rem; text-align:center;">
                                        <i class="fas fa-arrow-down"></i> ${formatBytes(data.dashboard_in)}
                                    </span>
                                    <span style="flex:1; background:#e8f0ff; color:#2e4db5; border-radius:5px; padding:4px 8px; font-size:0.75rem; text-align:center;">
                                        <i class="fas fa-arrow-up"></i> ${formatBytes(data.dashboard_out)}
                                    </span>
                                </div>
                            </div>`;
                    })
                    .catch(() => {
                        const el = document.getElementById('trafficStats');
                        if (el) el.innerHTML = '<div style="color:#adb5bd; font-size:0.78rem; text-align:center; padding:8px 0;">Unavailable</div>';
                    });
            }