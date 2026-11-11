using System.Diagnostics;
using System.Net.Http.Json;
using System.Text.Json;
using System.Text.Json.Serialization;

namespace UniversalClipboard.Services;

public sealed class BackendStatus
{
    [JsonPropertyName("connected")]
    public bool Connected { get; set; }

    [JsonPropertyName("state")]
    public string State { get; set; } = "starting";

    [JsonPropertyName("state_message")]
    public string StateMessage { get; set; } = "Connecting to Universal Clipboard service...";

    [JsonPropertyName("udid")]
    public string Udid { get; set; } = "";

    [JsonPropertyName("device_name")]
    public string DeviceName { get; set; } = "iPhone";

    [JsonPropertyName("product_type")]
    public string ProductType { get; set; } = "";

    [JsonPropertyName("ios_version")]
    public string IosVersion { get; set; } = "";

    [JsonPropertyName("active_transport")]
    public string ActiveTransport { get; set; } = "";

    [JsonPropertyName("transport_mode")]
    public string TransportMode { get; set; } = "auto";

    [JsonPropertyName("usb_connected")]
    public bool UsbConnected { get; set; }

    [JsonPropertyName("wifi_paired")]
    public bool WifiPaired { get; set; }

    [JsonPropertyName("ddi_mounted")]
    public bool DdiMounted { get; set; }

    [JsonPropertyName("last_change_count")]
    public long? LastChangeCount { get; set; }

    [JsonPropertyName("last_sync_ms")]
    public double? LastSyncMs { get; set; }

    [JsonPropertyName("total_synced")]
    public int TotalSynced { get; set; }

    [JsonPropertyName("history_revision")]
    public int HistoryRevision { get; set; }

    [JsonPropertyName("history_count")]
    public int HistoryCount { get; set; }
}

public sealed class ClipboardHistoryItem
{
    [JsonPropertyName("id")]
    public string Id { get; set; } = "";

    [JsonPropertyName("kind")]
    public string Kind { get; set; } = "text";

    [JsonPropertyName("uti")]
    public string Uti { get; set; } = "";

    [JsonPropertyName("text")]
    public string Text { get; set; } = "";

    [JsonPropertyName("file_name")]
    public string FileName { get; set; } = "";

    [JsonPropertyName("saved_file")]
    public string? SavedFile { get; set; }

    [JsonPropertyName("image_file")]
    public string? ImageFile { get; set; }

    [JsonPropertyName("width")]
    public int Width { get; set; }

    [JsonPropertyName("height")]
    public int Height { get; set; }

    [JsonPropertyName("raw_size")]
    public long RawSize { get; set; }

    [JsonPropertyName("change_count")]
    public long? ChangeCount { get; set; }

    [JsonPropertyName("transport")]
    public string Transport { get; set; } = "";

    [JsonPropertyName("source")]
    public string Source { get; set; } = "iphone";

    [JsonPropertyName("device_model")]
    public string DeviceModel { get; set; } = "iPhone";

    [JsonPropertyName("product_type")]
    public string ProductType { get; set; } = "iPhone";

    [JsonPropertyName("ios_version")]
    public string IosVersion { get; set; } = "";

    [JsonPropertyName("latency_ms")]
    public double LatencyMs { get; set; }

    [JsonPropertyName("timestamp")]
    public double Timestamp { get; set; }

    [JsonPropertyName("time_label")]
    public string TimeLabel { get; set; } = "";

    [JsonPropertyName("pinned")]
    public bool Pinned { get; set; }

    public bool IsImage => Kind == "image";
    public bool IsFile => Kind == "file";
    public bool IsLink => Kind == "url" || ExtractedUrl != null;
    public bool IsFromPc => string.Equals(Source, "pc", StringComparison.OrdinalIgnoreCase)
                            || string.Equals(DeviceModel, "This device", StringComparison.OrdinalIgnoreCase);
    public bool IsRichText => Uti.Contains("html", StringComparison.OrdinalIgnoreCase)
                              || Uti.Contains("webarchive", StringComparison.OrdinalIgnoreCase)
                              || Uti.Contains("rtf", StringComparison.OrdinalIgnoreCase);

    public string? ExtractedUrl
    {
        get
        {
            if (Kind == "url" && Uri.TryCreate(Text.Trim(), UriKind.Absolute, out _))
            {
                return Text.Trim();
            }
            if (string.IsNullOrWhiteSpace(Text)) return null;
            var match = System.Text.RegularExpressions.Regex.Match(Text, @"https?://[^\s<>""'）]+");
            return match.Success ? match.Value : null;
        }
    }

    public string? ExtractedDomain
    {
        get
        {
            var url = ExtractedUrl;
            if (!string.IsNullOrEmpty(url) && Uri.TryCreate(url, UriKind.Absolute, out var uri))
            {
                return uri.Host;
            }
            return null;
        }
    }

    public bool IsCode => !IsImage && !IsFile && !IsLink && (
        Text.Contains("function ", StringComparison.Ordinal)
        || Text.Contains("const ", StringComparison.Ordinal)
        || Text.Contains("import ", StringComparison.Ordinal)
        || Text.Contains("class ", StringComparison.Ordinal)
        || Text.Contains("def ", StringComparison.Ordinal)
        || Text.StartsWith("sk-", StringComparison.Ordinal)
        || Text.StartsWith("/tmp/", StringComparison.Ordinal)
        || (Text.Contains('{') && Text.Contains('}')));

    public string ShortDeviceName
    {
        get
        {
            if (IsFromPc) return "This device";
            if (string.IsNullOrWhiteSpace(DeviceModel)) return "iPhone";
            var parenIdx = DeviceModel.IndexOf('(');
            return parenIdx > 0 ? DeviceModel[..parenIdx].Trim() : DeviceModel;
        }
    }

    public string ImageDisplayName
    {
        get
        {
            if (!string.IsNullOrWhiteSpace(FileName) && !FileName.StartsWith("Clipboard_")) return FileName;
            if (!string.IsNullOrWhiteSpace(Text) && Text != "Image" && !Text.StartsWith("Clipboard_")) return Text;
            return Width > 0 && Height > 0 ? $"Image {Width} × {Height}" : "Image";
        }
    }

    public string FileDisplayName
    {
        get
        {
            if (!string.IsNullOrWhiteSpace(FileName)) return FileName;
            if (!string.IsNullOrWhiteSpace(Text)) return Text;
            return IsImage ? ImageDisplayName : "File";
        }
    }

    public string FileExtensionBadge
    {
        get
        {
            var ext = Path.GetExtension(FileDisplayName).TrimStart('.').ToUpperInvariant();
            if (!string.IsNullOrWhiteSpace(ext) && ext.Length <= 5) return ext;
            if (Uti.Contains("zip", StringComparison.OrdinalIgnoreCase)) return "ZIP";
            if (Uti.Contains("pdf", StringComparison.OrdinalIgnoreCase)) return "PDF";
            if (Uti.Contains("json", StringComparison.OrdinalIgnoreCase)) return "JSON";
            if (Uti.Contains("mp4", StringComparison.OrdinalIgnoreCase) || Uti.Contains("mpeg-4", StringComparison.OrdinalIgnoreCase)) return "MP4";
            if (Uti.Contains("mp3", StringComparison.OrdinalIgnoreCase)) return "MP3";
            return "FILE";
        }
    }

    public string RelativeTime
    {
        get
        {
            if (Timestamp <= 0) return TimeLabel;
            var now = DateTimeOffset.UtcNow.ToUnixTimeSeconds();
            var diff = Math.Max(0, now - (long)Timestamp);
            if (diff < 60) return "Just now";
            if (diff < 3600) return $"{diff / 60}m ago";
            if (diff < 86400) return $"{diff / 3600}h ago";
            return $"{diff / 86400}d ago";
        }
    }

    public bool IsToday
    {
        get
        {
            if (Timestamp <= 0) return true;
            var dt = DateTimeOffset.FromUnixTimeSeconds((long)Timestamp).ToLocalTime();
            return dt.Date == DateTimeOffset.Now.Date;
        }
    }

    public string FormattedSize
    {
        get
        {
            if (RawSize < 1024) return $"{RawSize} B";
            if (RawSize < 1024 * 1024) return $"{RawSize / 1024.0:F1} KB";
            return $"{RawSize / (1024.0 * 1024.0):F2} MB";
        }
    }
}

public sealed class LogsResponse
{
    [JsonPropertyName("ok")]
    public bool Ok { get; set; }

    [JsonPropertyName("count")]
    public int Count { get; set; }

    [JsonPropertyName("logs")]
    public List<string> Logs { get; set; } = [];
}

public sealed class HistoryResponse
{
    [JsonPropertyName("revision")]
    public int Revision { get; set; }

    [JsonPropertyName("items")]
    public List<ClipboardHistoryItem> Items { get; set; } = [];
}

public sealed class ClipboardBackendService : IDisposable
{
    private const string BaseUrl = "http://127.0.0.1:8767";
    private readonly HttpClient _http = new() { Timeout = TimeSpan.FromSeconds(4) };
    private readonly HttpClient _longHttp = new() { Timeout = TimeSpan.FromSeconds(30) };
    private Process? _ownedProcess;

    public string GetImageUrl(string itemId) => $"{BaseUrl}/api/history/{Uri.EscapeDataString(itemId)}/image";

    public async Task EnsureBackendRunningAsync()
    {
        if (await IsReachableAsync())
        {
            return;
        }

        var scriptPath = ResolveBackendScriptPath();
        if (scriptPath is null)
        {
            return;
        }

        var pythonExe = ResolvePythonExecutable();
        var psi = new ProcessStartInfo
        {
            FileName = pythonExe,
            Arguments = $"-u \"{scriptPath}\" --transport auto --sync-on-connect",
            WorkingDirectory = Path.GetDirectoryName(scriptPath) ?? AppContext.BaseDirectory,
            UseShellExecute = false,
            CreateNoWindow = true,
            RedirectStandardOutput = false,
            RedirectStandardError = false,
        };

        try
        {
            _ownedProcess = Process.Start(psi);
        }
        catch
        {
            // Ignore launch failure; UI will surface status appropriately
        }
    }

    public async Task<bool> IsReachableAsync()
    {
        try
        {
            using var resp = await _http.GetAsync($"{BaseUrl}/api/status");
            return resp.IsSuccessStatusCode;
        }
        catch
        {
            return false;
        }
    }

    public async Task<BackendStatus?> GetStatusAsync()
    {
        try
        {
            return await _http.GetFromJsonAsync<BackendStatus>($"{BaseUrl}/api/status");
        }
        catch
        {
            return null;
        }
    }

    public async Task<HistoryResponse?> GetHistoryAsync()
    {
        try
        {
            return await _http.GetFromJsonAsync<HistoryResponse>($"{BaseUrl}/api/history");
        }
        catch
        {
            return null;
        }
    }

    public async Task<bool> CopyItemToWindowsAsync(string itemId)
    {
        try
        {
            using var resp = await _http.PostAsync($"{BaseUrl}/api/history/{Uri.EscapeDataString(itemId)}/copy", null);
            return resp.IsSuccessStatusCode;
        }
        catch
        {
            return false;
        }
    }

    public async Task<bool> SendItemToIphoneAsync(string itemId)
    {
        try
        {
            using var resp = await _longHttp.PostAsync($"{BaseUrl}/api/history/{Uri.EscapeDataString(itemId)}/send", null);
            return resp.IsSuccessStatusCode;
        }
        catch
        {
            return false;
        }
    }

    public async Task<bool?> TogglePinAsync(string itemId)
    {
        try
        {
            using var resp = await _http.PostAsync($"{BaseUrl}/api/history/{Uri.EscapeDataString(itemId)}/pin", null);
            if (!resp.IsSuccessStatusCode) return null;
            using var doc = JsonDocument.Parse(await resp.Content.ReadAsStringAsync());
            if (doc.RootElement.TryGetProperty("pinned", out var prop))
            {
                return prop.GetBoolean();
            }
            return null;
        }
        catch
        {
            return null;
        }
    }

    public async Task<bool> DeleteItemAsync(string itemId)
    {
        try
        {
            using var resp = await _http.PostAsync($"{BaseUrl}/api/history/{Uri.EscapeDataString(itemId)}/delete", null);
            return resp.IsSuccessStatusCode;
        }
        catch
        {
            return false;
        }
    }

    public async Task<bool> ClearHistoryAsync(bool includePinned = false)
    {
        try
        {
            using var resp = await _http.PostAsJsonAsync($"{BaseUrl}/api/history/clear", new { include_pinned = includePinned });
            return resp.IsSuccessStatusCode;
        }
        catch
        {
            return false;
        }
    }

    public async Task<List<string>> GetBackendLogsAsync(int tail = 300)
    {
        try
        {
            var res = await _http.GetFromJsonAsync<LogsResponse>($"{BaseUrl}/api/logs?tail={tail}");
            return res?.Logs ?? [];
        }
        catch
        {
            return [];
        }
    }

    public async Task<bool> ClearBackendLogsAsync()
    {
        try
        {
            using var resp = await _http.PostAsync($"{BaseUrl}/api/logs/clear", null);
            return resp.IsSuccessStatusCode;
        }
        catch
        {
            return false;
        }
    }

    public async Task<bool> TriggerSyncNowAsync()
    {
        try
        {
            using var resp = await _http.PostAsync($"{BaseUrl}/api/sync-now", null);
            return resp.IsSuccessStatusCode;
        }
        catch
        {
            return false;
        }
    }

    public async Task<bool> SetTransportModeAsync(string mode)
    {
        try
        {
            using var resp = await _http.PostAsJsonAsync($"{BaseUrl}/api/transport", new { mode });
            return resp.IsSuccessStatusCode;
        }
        catch
        {
            return false;
        }
    }

    public async Task<(bool Ok, string Message)> PairWifiOverUsbAsync()
    {
        try
        {
            using var resp = await _longHttp.PostAsync($"{BaseUrl}/api/pair-wifi", null);
            var json = await resp.Content.ReadAsStringAsync();
            using var doc = JsonDocument.Parse(json);
            if (resp.IsSuccessStatusCode && doc.RootElement.TryGetProperty("ok", out var okProp) && okProp.GetBoolean())
            {
                var udid = doc.RootElement.TryGetProperty("udid", out var u) ? u.GetString() : "";
                return (true, $"Paired iPhone ({udid}) for wireless sync.");
            }
            var err = doc.RootElement.TryGetProperty("error", out var e) ? e.GetString() : "USB pairing failed.";
            return (false, err ?? "USB pairing failed.");
        }
        catch (Exception ex)
        {
            return (false, ex.Message);
        }
    }

    private static string? ResolveBackendScriptPath()
    {
        var dir = new DirectoryInfo(AppContext.BaseDirectory);
        for (var i = 0; i < 6 && dir is not null; i++, dir = dir.Parent)
        {
            var bridgeServiceCandidate = Path.Combine(dir.FullName, "CoreDeviceBridge", "bridgeService.py");
            if (File.Exists(bridgeServiceCandidate))
            {
                return bridgeServiceCandidate;
            }
            var universalClipCandidate = Path.Combine(dir.FullName, "universalClipboard.py");
            if (File.Exists(universalClipCandidate))
            {
                return universalClipCandidate;
            }
        }

        const string fallbackBridge = @"d:\UniversalClipboard\CoreDeviceBridge\bridgeService.py";
        if (File.Exists(fallbackBridge)) return fallbackBridge;
        const string fallbackUniversal = @"d:\UniversalClipboard\universalClipboard.py";
        return File.Exists(fallbackUniversal) ? fallbackUniversal : null;
    }

    private static string ResolvePythonExecutable()
    {
        var localAppData = Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData);
        var py315 = Path.Combine(localAppData, "Python", "pythoncore-3.15-64", "python.exe");
        if (File.Exists(py315))
        {
            return py315;
        }
        return "python";
    }

    public void Dispose()
    {
        _http.Dispose();
        _longHttp.Dispose();
        if (_ownedProcess is { HasExited: false })
        {
            try
            {
                _ownedProcess.Kill(entireProcessTree: true);
            }
            catch
            {
                // Ignore
            }
        }
    }
}
