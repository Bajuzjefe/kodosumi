# Kodosumi Analytics Dashboard

An integrated analytics and monitoring dashboard for Kodosumi that provides comprehensive insights into agent executions, errors, and system performance.

## Features

### 1. Running Agents Overview
- Comprehensive view of all active and historical agent executions
- Multi-column table with:
  - **Agent Name**: First column for easy identification
  - **Execution ID**: Unique identifier for each execution
  - **User**: User name (resolved from admin.db)
  - **Status**: Color-coded badges (running, awaiting, finished, error)
  - **Started**: Timestamp when execution began
  - **Runtime**: Execution duration
  - **Files**: Upload/download counts with clickable links
  - **Actions**: Link to detailed execution view
- Advanced filtering:
  - Global free-text search
  - Agent name dropdown
  - User dropdown
  - Status dropdown
  - Time range selector (1h to all time)
- Statistics display "filtered / total" counts
- File modal for viewing and downloading execution files

### 2. Error Tracking
- Comprehensive error log aggregation across all executions
- Time-based filtering (1h, 6h, 24h, 7 days)
- Click-through to detailed error information including:
  - Full error messages and stack traces
  - Execution metadata
  - Timestamp and user context
- Visual error cards for easy scanning

### 3. Execution Timeline
- Interactive D3.js visualization of execution history
- Hourly aggregation showing:
  - Total executions per time period
  - Running vs finished states
  - Error occurrences
- Time range filtering

### 4. Statistical Analytics
- **Summary Statistics**:
  - Total executions count
  - Currently running agents
  - Error rate percentage
  - Average runtime

- **Status Distribution**: Pie chart showing executions by status (running, finished, error, etc.)
- **User Activity**: Pie chart showing executions per user

## Technical Architecture

### Backend (Python)
- **Location**: `/kodosumi/service/dashboard.py`
- **Framework**: Litestar Controller
- **API Endpoints**:
  - `GET /api/dashboard/running-agents` - Active and recent executions with filtering
    - Query params: `hours`, `agent_name`, `user`, `status`, `search`
  - `GET /api/dashboard/errors?hours={n}` - Recent errors with time filtering
  - `GET /api/dashboard/timeline?hours={n}` - Timeline data for charting
  - `GET /api/dashboard/agent-stats` - Aggregate statistics
  - `GET /api/dashboard/execution/{fid}/details` - Detailed execution data
  - `GET /api/dashboard/execution/{fid}/files` - List upload/download files with URLs

### Frontend
- **Location**: `/kodosumi/service/admin/templates/dashboard/`
- **Stack**:
  - Vanilla JavaScript (ES6)
  - D3.js v7 for data visualizations
  - Beer CSS for Material Design styling
  - Jinja2 templating
- **Files**:
  - `dashboard.html` - Main template with tab navigation
  - `dashboard.js` - Client-side logic and API integration

### Data Sources
The dashboard aggregates data from:
- **Execution Databases**: `./data/execution/{user_id}/{execution_id}/sqlite3.db` (or `db.sqlite` for legacy)
  - Each execution has its own SQLite database
  - Contains a `monitor` table with timestamped events
- **Monitor Table Schema**:
  - `id`: Auto-increment primary key
  - `timestamp`: Unix timestamp (float)
  - `kind`: Event type (status, error, meta, inputs, result, action, final, upload, etc.)
  - `message`: Event data
    - **status**: Plain string values ("running", "finished", "error", "awaiting", "starting")
    - **meta**: JSON object with agent metadata
    - **error**: Plain text or JSON with error details
- **Admin Database**: `./data/admin.db` - User information (roles table)
- **File System**: Upload files in `in/` and download files in `out/` subdirectories

## Usage

### Accessing the Dashboard
1. Start Kodosumi admin panel:
   ```bash
   koco start
   ```
   The admin panel starts by default at http://localhost:3370

2. Navigate to: http://localhost:3370/admin/dashboard

3. Login with admin credentials (default: admin/admin)

### Navigation
The dashboard is accessible via the main navigation:
- **Icon**: Dashboard icon
- **Label**: Analytics
- **Location**: In the left sidebar navigation

### Filtering Options
The dashboard provides multiple filter controls:
- **Global Search**: Free-text search across all fields
- **Agent Name**: Filter by specific agent
- **User**: Filter by user
- **Status**: Filter by execution status (running, finished, error, awaiting, etc.)
- **Time Range**: Select time window (1h, 6h, 24h, 7 days, 30 days, or all time)

### Features
- **Filtered Statistics**: Shows "filtered / total" counts (e.g., "12 / 56 executions")
- **File Access**: Click upload/download counts to view and download execution files
- **Manual Refresh**: Use refresh button to update data immediately

## API Response Examples

### Running Agents
```json
{
  "total": 5,
  "agents": [
    {
      "fid": "1234567890",
      "user_id": "user123",
      "status": "running",
      "start_time": 1700000000.0,
      "end_time": null,
      "runtime": 45.2,
      "error": null,
      "meta": {"entry": "my_agent"}
    }
  ]
}
```

### Errors
```json
{
  "total": 3,
  "errors": [
    {
      "fid": "1234567890",
      "user_id": "user123",
      "timestamp": 1700000100.0,
      "error": {
        "error": "ValueError: Invalid input",
        "traceback": "..."
      }
    }
  ]
}
```

### Agent Stats
```json
{
  "total_executions": 150,
  "by_status": {
    "finished": 120,
    "running": 5,
    "error": 25
  },
  "by_user": {
    "user123": 80,
    "user456": 70
  },
  "avg_runtime": 127.5,
  "error_rate": 0.167
}
```

## Customization

### Adding New Metrics
To add new metrics to the dashboard:

1. **Backend**: Add new endpoint in `dashboard.py`
```python
@get("/custom-metric")
async def get_custom_metric(self, state: State) -> Dict[str, Any]:
    # Your custom logic
    return {"data": ...}
```

2. **Frontend**: Add fetch and render functions in `dashboard.js`
```javascript
async function fetchCustomMetric() {
    const response = await fetch('/api/dashboard/custom-metric');
    return await response.json();
}

function renderCustomMetric(data) {
    // Your rendering logic
}
```

3. **Template**: Add UI elements in `dashboard.html`

### Styling
The dashboard uses Beer CSS with Kodosumi's custom theme variables:
- `--primary`: Main theme color
- `--error`: Error state color
- `--surface`: Background color
- Theme automatically switches between light/dark modes

## Performance Considerations

- **Database Queries**: Optimized to scan execution databases in parallel
- **Caching**: Consider adding caching for frequently accessed statistics
- **Time Range**: Limit time ranges for large deployments to prevent slow queries
- **Pagination**: Error lists are limited to 100 entries by default

## Future Enhancements

Completed features:
- [x] Advanced filtering (by agent name, user, status, global search)
- [x] File upload/download access and viewing

Potential improvements:
- [ ] Real-time WebSocket updates instead of polling
- [ ] Export data to CSV/JSON
- [ ] Custom date range selection
- [ ] Alert configuration for error thresholds
- [ ] Execution comparison views
- [ ] Performance metrics (CPU, memory usage if available)
- [ ] Log aggregation and search

## Troubleshooting

### Dashboard not loading
- Check that Kodosumi server is running
- Verify authentication credentials
- Check browser console for JavaScript errors

### No data displayed
- Ensure executions exist in `./data/execution/`
- Verify database files are readable
- Check API endpoint responses in browser DevTools

### Performance issues
- Reduce time range filter
- Check database file sizes
- Monitor server logs for slow queries

## Contributing

To contribute improvements to the dashboard:
1. Test changes with various data volumes
2. Ensure mobile responsiveness
3. Follow existing code style and patterns
4. Update this documentation
