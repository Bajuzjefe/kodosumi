// Dashboard JavaScript
let refreshInterval = null;
let currentTimeRange = 24;
let currentFilters = {
    search: '',
    agent_name: '',
    user: '',
    status: ''
};
let globalStats = {
    total: 0,
    running: 0,
    error_rate: 0,
    avg_runtime: 0
};

// Utility functions
function formatTimestamp(timestamp) {
    if (!timestamp) return 'N/A';
    const date = new Date(timestamp * 1000);
    return date.toLocaleString();
}

function formatDuration(seconds) {
    if (!seconds) return 'N/A';
    if (seconds < 60) return `${seconds.toFixed(1)}s`;
    if (seconds < 3600) return `${(seconds / 60).toFixed(1)}m`;
    return `${(seconds / 3600).toFixed(1)}h`;
}

function getStatusBadge(status) {
    const statusLower = status.toLowerCase();
    const badges = {
        'running': '<span class="badge primary">Running</span>',
        'awaiting': '<span class="badge secondary">Awaiting</span>',
        'finished': '<span class="badge tertiary">Finished</span>',
        'error': '<span class="badge error">Error</span>',
        'starting': '<span class="badge">Starting</span>'
    };
    return badges[statusLower] || `<span class="badge">${status}</span>`;
}

// API calls
async function fetchRunningAgents() {
    const params = new URLSearchParams({
        hours: currentTimeRange,
        ...Object.fromEntries(Object.entries(currentFilters).filter(([_, v]) => v !== ''))
    });
    const response = await fetch(`/api/dashboard/running-agents?${params}`);
    return await response.json();
}

async function fetchErrors() {
    const response = await fetch(`/api/dashboard/errors?hours=${currentTimeRange}`);
    return await response.json();
}

async function fetchTimeline() {
    const response = await fetch(`/api/dashboard/timeline?hours=${currentTimeRange}`);
    return await response.json();
}

async function fetchAgentStats() {
    const response = await fetch('/api/dashboard/agent-stats');
    return await response.json();
}

async function fetchExecutionDetails(fid) {
    const response = await fetch(`/api/dashboard/execution/${fid}/details`);
    return await response.json();
}

async function fetchExecutionFiles(fid) {
    const response = await fetch(`/api/dashboard/execution/${fid}/files`);
    return await response.json();
}

function formatFileSize(bytes) {
    if (bytes < 1024) return bytes + ' B';
    if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB';
    return (bytes / (1024 * 1024)).toFixed(1) + ' MB';
}

// Render functions
function renderRunningAgents(data) {
    const tbody = document.querySelector('#running-agents-table tbody');
    const resultsCount = document.getElementById('results-count');

    if (data.agents.length === 0) {
        tbody.innerHTML = '<tr><td colspan="7" class="center-align">No agents found</td></tr>';
        resultsCount.textContent = 'No results';
        return;
    }

    // Populate filter dropdowns
    populateFilters(data.agents);

    tbody.innerHTML = data.agents.map(agent => {
        const hasFiles = agent.upload_count > 0 || agent.download_count > 0;
        const fileDisplay = hasFiles
            ? `<a href="#" onclick="showFiles('${agent.fid}'); return false;" style="color: var(--primary); text-decoration: underline;">${agent.upload_count}/${agent.download_count}</a>`
            : `<span style="color: var(--on-surface-variant);">${agent.upload_count}/${agent.download_count}</span>`;

        return `
        <tr>
            <td><strong>${agent.agent_name}</strong></td>
            <td><code>${agent.fid}</code></td>
            <td>${agent.user_name || agent.user_id}</td>
            <td class="center-align">${getStatusBadge(agent.status)}</td>
            <td>${formatTimestamp(agent.start_time)}</td>
            <td>${formatDuration(agent.runtime)}</td>
            <td class="center-align">${fileDisplay}</td>
            <td>
                <a href="/admin/status/view/${agent.fid}" class="button small">
                    <i class="tiny">visibility</i>
                </a>
            </td>
        </tr>
        `;
    }).join('');

    // Update results count
    resultsCount.textContent = `Showing ${data.agents.length} of ${data.total} executions`;
}

async function showFiles(fid) {
    const modal = document.getElementById('files-modal');
    const modalFid = document.getElementById('files-modal-fid');
    const uploadList = document.getElementById('upload-files-list');
    const downloadList = document.getElementById('download-files-list');
    const uploadCount = document.getElementById('upload-count');
    const downloadCount = document.getElementById('download-count');

    modalFid.textContent = fid;
    uploadList.innerHTML = '<p>Loading...</p>';
    downloadList.innerHTML = '<p>Loading...</p>';
    modal.showModal();

    try {
        const data = await fetchExecutionFiles(fid);

        uploadCount.textContent = data.upload_count;
        downloadCount.textContent = data.download_count;

        // Render uploads
        if (data.uploads.length === 0) {
            uploadList.innerHTML = '<p class="small" style="text-align: center; color: var(--on-surface-variant); padding: 20px;">No uploaded files</p>';
        } else {
            uploadList.innerHTML = data.uploads.map(file => `
                <div class="file-card">
                    <div class="file-name" title="${file.name}">${file.name}</div>
                    <div class="file-size">${formatFileSize(file.size)}</div>
                    <a href="${file.url}" download class="download-btn">
                        <i>download</i>
                    </a>
                </div>
            `).join('');
        }

        // Render downloads
        if (data.downloads.length === 0) {
            downloadList.innerHTML = '<p class="small" style="text-align: center; color: var(--on-surface-variant); padding: 20px;">No output files</p>';
        } else {
            downloadList.innerHTML = data.downloads.map(file => `
                <div class="file-card">
                    <div class="file-name" title="${file.name}">${file.name}</div>
                    <div class="file-size">${formatFileSize(file.size)}</div>
                    <a href="${file.url}" download class="download-btn">
                        <i>download</i>
                    </a>
                </div>
            `).join('');
        }
    } catch (err) {
        uploadList.innerHTML = `<p class="error-text">Failed to load files: ${err.message}</p>`;
        downloadList.innerHTML = '';
    }
}

function populateFilters(agents) {
    // Get unique values
    const agentNames = [...new Set(agents.map(a => a.agent_name))].sort();
    const users = [...new Set(agents.map(a => a.user_name || a.user_id))].sort();
    const statuses = [...new Set(agents.map(a => a.status))].sort();

    // Populate agent filter
    const agentSelect = document.getElementById('filter-agent');
    const currentAgent = agentSelect.value;
    agentSelect.innerHTML = '<option value="">All Agents</option>' +
        agentNames.map(name => `<option value="${name}" ${name === currentAgent ? 'selected' : ''}>${name}</option>`).join('');

    // Populate user filter
    const userSelect = document.getElementById('filter-user');
    const currentUser = userSelect.value;
    userSelect.innerHTML = '<option value="">All Users</option>' +
        users.map(user => `<option value="${user}" ${user === currentUser ? 'selected' : ''}>${user}</option>`).join('');

    // Populate status filter - only show statuses that exist in the data
    const statusSelect = document.getElementById('filter-status');
    const currentStatus = statusSelect.value;
    statusSelect.innerHTML = '<option value="">All Status</option>' +
        statuses.map(status => `<option value="${status}" ${status === currentStatus ? 'selected' : ''}>${status.charAt(0).toUpperCase() + status.slice(1)}</option>`).join('');
}

function renderErrors(data) {
    const container = document.getElementById('errors-container');

    if (data.errors.length === 0) {
        container.innerHTML = '<p class="center-align">No errors in selected time range</p>';
        return;
    }

    container.innerHTML = data.errors.map(error => {
        const errorMsg = error.error.error || error.error.message || JSON.stringify(error.error);
        const errorPreview = errorMsg.substring(0, 200) + (errorMsg.length > 200 ? '...' : '');

        return `
            <article class="border small-padding small-margin" style="cursor: pointer;"
                     onclick="showErrorDetail('${error.fid}', ${error.timestamp})">
                <nav class="no-space">
                    <div class="max">
                        <h6 class="small error-text">${error.fid}</h6>
                        <p class="small">${formatTimestamp(error.timestamp)} - User: ${error.user_id}</p>
                    </div>
                    <i>chevron_right</i>
                </nav>
                <p class="small"><code>${errorPreview}</code></p>
            </article>
        `;
    }).join('');
}

async function showErrorDetail(fid, timestamp) {
    const modal = document.getElementById('error-detail-modal');
    const content = document.getElementById('error-detail-content');

    content.innerHTML = '<p>Loading...</p>';
    modal.showModal();

    try {
        const data = await fetchExecutionDetails(fid);
        const errorEvent = data.events.find(e => e.kind === 'error' && e.timestamp === timestamp);

        if (errorEvent) {
            const errorData = JSON.parse(errorEvent.message);
            content.innerHTML = `
                <p><strong>Execution ID:</strong> <code>${fid}</code></p>
                <p><strong>User:</strong> ${data.metadata.user_id}</p>
                <p><strong>Time:</strong> ${formatTimestamp(timestamp)}</p>
                <div class="small-divider"></div>
                <h6>Error Message:</h6>
                <pre style="background: var(--surface-container); padding: 16px; border-radius: 4px; overflow-x: auto;">${JSON.stringify(errorData, null, 2)}</pre>
            `;
        }
    } catch (err) {
        content.innerHTML = `<p class="error-text">Failed to load error details: ${err.message}</p>`;
    }
}

function renderTimeline(data) {
    const container = document.getElementById('timeline-chart');

    if (data.executions.length === 0) {
        container.innerHTML = '<p class="center-align">No executions in selected time range</p>';
        return;
    }

    // Group executions by hour
    const hourlyData = {};
    data.executions.forEach(exec => {
        const hour = new Date(exec.start_time * 1000);
        hour.setMinutes(0, 0, 0);
        const hourKey = hour.toISOString();

        if (!hourlyData[hourKey]) {
            hourlyData[hourKey] = { total: 0, errors: 0, running: 0, finished: 0 };
        }

        hourlyData[hourKey].total++;
        if (exec.error) hourlyData[hourKey].errors++;
        if (exec.status === 'running') hourlyData[hourKey].running++;
        if (exec.status === 'finished') hourlyData[hourKey].finished++;
    });

    // Create D3 visualization
    const margin = { top: 20, right: 30, bottom: 50, left: 50 };
    const width = container.clientWidth - margin.left - margin.right;
    const height = 400 - margin.top - margin.bottom;

    container.innerHTML = '';
    const svg = d3.select('#timeline-chart')
        .append('svg')
        .attr('width', width + margin.left + margin.right)
        .attr('height', height + margin.top + margin.bottom)
        .append('g')
        .attr('transform', `translate(${margin.left},${margin.top})`);

    const dataArray = Object.entries(hourlyData)
        .map(([hour, counts]) => ({ hour: new Date(hour), ...counts }))
        .sort((a, b) => a.hour - b.hour);

    // X scale
    const x = d3.scaleTime()
        .domain(d3.extent(dataArray, d => d.hour))
        .range([0, width]);

    // Y scale
    const y = d3.scaleLinear()
        .domain([0, d3.max(dataArray, d => d.total)])
        .range([height, 0]);

    // Add axes
    svg.append('g')
        .attr('transform', `translate(0,${height})`)
        .call(d3.axisBottom(x).ticks(6))
        .selectAll('text')
        .style('fill', 'var(--on-surface)');

    svg.append('g')
        .call(d3.axisLeft(y))
        .selectAll('text')
        .style('fill', 'var(--on-surface)');

    // Add line
    const line = d3.line()
        .x(d => x(d.hour))
        .y(d => y(d.total));

    svg.append('path')
        .datum(dataArray)
        .attr('fill', 'none')
        .attr('stroke', 'var(--primary)')
        .attr('stroke-width', 2)
        .attr('d', line);

    // Add points
    svg.selectAll('dot')
        .data(dataArray)
        .enter()
        .append('circle')
        .attr('cx', d => x(d.hour))
        .attr('cy', d => y(d.total))
        .attr('r', 4)
        .attr('fill', 'var(--primary)');
}

function renderStats(data) {
    // Store global stats for comparison
    globalStats = {
        total: data.total_executions,
        running: data.by_status.running || 0,
        error_rate: data.error_rate,
        avg_runtime: data.avg_runtime
    };

    // Update summary stats (will be updated again with filtered counts)
    updateStatsDisplay();

    // Render status pie chart
    renderPieChart('status-chart', data.by_status);

    // Render user pie chart
    renderPieChart('user-chart', data.by_user);
}

function updateStatsDisplay(filteredData) {
    if (filteredData) {
        // Show filtered / total
        const filteredTotal = filteredData.agents.length;
        const filteredRunning = filteredData.agents.filter(a => a.status === 'running').length;
        const filteredErrors = filteredData.agents.filter(a => a.error).length;
        const filteredErrorRate = filteredTotal > 0 ? (filteredErrors / filteredTotal) * 100 : 0;
        const filteredAvgRuntime = filteredData.agents.reduce((sum, a) => sum + (a.runtime || 0), 0) / (filteredTotal || 1);

        document.getElementById('stat-total').innerHTML = `${filteredTotal}<span class="small" style="color: var(--on-surface-variant);"> / ${globalStats.total}</span>`;
        document.getElementById('stat-running').innerHTML = `${filteredRunning}<span class="small" style="color: var(--on-surface-variant);"> / ${globalStats.running}</span>`;
        document.getElementById('stat-errors').innerHTML = `${filteredErrorRate.toFixed(1)}%<span class="small" style="color: var(--on-surface-variant);"> / ${(globalStats.error_rate * 100).toFixed(1)}%</span>`;
        document.getElementById('stat-runtime').innerHTML = `${formatDuration(filteredAvgRuntime)}<span class="small" style="color: var(--on-surface-variant);"> / ${formatDuration(globalStats.avg_runtime)}</span>`;
    } else {
        // Show only totals
        document.getElementById('stat-total').textContent = globalStats.total;
        document.getElementById('stat-running').textContent = globalStats.running;
        document.getElementById('stat-errors').textContent = `${(globalStats.error_rate * 100).toFixed(1)}%`;
        document.getElementById('stat-runtime').textContent = formatDuration(globalStats.avg_runtime);
    }
}

function renderPieChart(containerId, data) {
    const container = document.getElementById(containerId);
    const width = container.clientWidth;
    const height = 300;
    const radius = Math.min(width, height) / 2 - 40;

    container.innerHTML = '';
    const svg = d3.select(`#${containerId}`)
        .append('svg')
        .attr('width', width)
        .attr('height', height)
        .append('g')
        .attr('transform', `translate(${width / 2},${height / 2})`);

    const color = d3.scaleOrdinal()
        .range(['#c4fe0a', '#679800', '#b3d189', '#a0a77e', '#ff4d4d']);

    const pie = d3.pie()
        .value(d => d[1]);

    const arc = d3.arc()
        .innerRadius(0)
        .outerRadius(radius);

    const arcs = svg.selectAll('arc')
        .data(pie(Object.entries(data)))
        .enter()
        .append('g');

    arcs.append('path')
        .attr('d', arc)
        .attr('fill', (d, i) => color(i));

    arcs.append('text')
        .attr('transform', d => `translate(${arc.centroid(d)})`)
        .attr('text-anchor', 'middle')
        .style('fill', 'var(--on-surface)')
        .style('font-size', '12px')
        .text(d => `${d.data[0]}: ${d.data[1]}`);
}

// Load all dashboard data
async function loadDashboard() {
    try {
        // Load all data in parallel
        const [runningData, errorData, timelineData, statsData] = await Promise.all([
            fetchRunningAgents(),
            fetchErrors(),
            fetchTimeline(),
            fetchAgentStats()
        ]);

        // Render all components
        renderRunningAgents(runningData);
        renderErrors(errorData);
        renderTimeline(timelineData);
        renderStats(statsData);

        // Update stats with filtered data
        updateStatsDisplay(runningData);
    } catch (err) {
        console.error('Failed to load dashboard:', err);
    }
}

// Event listeners
document.addEventListener('DOMContentLoaded', () => {
    loadDashboard();

    // Refresh button
    document.getElementById('refresh-dashboard').addEventListener('click', (e) => {
        e.preventDefault();
        loadDashboard();
    });

    // Time range selector
    document.getElementById('time-range').addEventListener('change', (e) => {
        currentTimeRange = parseInt(e.target.value);
        loadDashboard();
    });

    // Filter event listeners
    document.getElementById('filter-search').addEventListener('input', (e) => {
        currentFilters.search = e.target.value;
        loadDashboard();
    });

    document.getElementById('filter-agent').addEventListener('change', (e) => {
        currentFilters.agent_name = e.target.value;
        loadDashboard();
    });

    document.getElementById('filter-user').addEventListener('change', (e) => {
        currentFilters.user = e.target.value;
        loadDashboard();
    });

    document.getElementById('filter-status').addEventListener('change', (e) => {
        currentFilters.status = e.target.value;
        loadDashboard();
    });

    // Auto-refresh every 30 seconds
    refreshInterval = setInterval(loadDashboard, 30000);
});

// Cleanup on page unload
window.addEventListener('beforeunload', () => {
    if (refreshInterval) {
        clearInterval(refreshInterval);
    }
});

// Make functions available globally
window.showErrorDetail = showErrorDetail;
window.showFiles = showFiles;
