using System.Text.Json.Serialization;

namespace UniversalClipboard.Services;

public sealed class HardwareCapabilityInfo
{
    [JsonPropertyName("bluetooth_available")]
    public bool BluetoothAvailable { get; set; }

    [JsonPropertyName("bluetooth_le_supported")]
    public bool BluetoothLeSupported { get; set; }

    [JsonPropertyName("bluetooth_peripheral_supported")]
    public bool BluetoothPeripheralSupported { get; set; }

    [JsonPropertyName("bluetooth_adapter_name")]
    public string BluetoothAdapterName { get; set; } = "Unknown";

    [JsonPropertyName("wifi_adapter_name")]
    public string WifiAdapterName { get; set; } = "Intel(R) Wi-Fi 6E AX211";

    [JsonPropertyName("awdl_dedicated_adapter_detected")]
    public bool AwdlDedicatedAdapterDetected { get; set; }

    [JsonPropertyName("status_summary")]
    public string StatusSummary { get; set; } = "Standby";

    public string AwdlGuidanceMessage => AwdlDedicatedAdapterDetected
        ? "Dedicated AWDL wireless frame injector detected."
        : "Intel AX211 connects via Local Wi-Fi & mDNS. For zero-config direct Apple AWDL ad-hoc mesh, a supported USB Wi-Fi adapter (e.g. TP-Link Archer T3U) can be attached.";
}

public sealed class AirDropTransferItem
{
    [JsonPropertyName("id")]
    public string Id { get; set; } = "";

    [JsonPropertyName("sender_name")]
    public string SenderName { get; set; } = "iPhone";

    [JsonPropertyName("file_names")]
    public List<string> FileNames { get; set; } = [];

    [JsonPropertyName("total_bytes")]
    public long TotalBytes { get; set; }

    [JsonPropertyName("received_bytes")]
    public long ReceivedBytes { get; set; }

    [JsonPropertyName("status")]
    public string Status { get; set; } = "pending_consent";

    [JsonPropertyName("timestamp")]
    public double Timestamp { get; set; }

    [JsonPropertyName("time_label")]
    public string TimeLabel { get; set; } = "";

    [JsonPropertyName("saved_paths")]
    public List<string> SavedPaths { get; set; } = [];

    [JsonPropertyName("error_message")]
    public string? ErrorMessage { get; set; }

    public bool IsPendingConsent => string.Equals(Status, "pending_consent", StringComparison.OrdinalIgnoreCase);
    public bool IsTransferring => string.Equals(Status, "transferring", StringComparison.OrdinalIgnoreCase);
    public bool IsCompleted => string.Equals(Status, "completed", StringComparison.OrdinalIgnoreCase);
    public bool IsDeclined => string.Equals(Status, "declined", StringComparison.OrdinalIgnoreCase);
    public bool IsFailed => string.Equals(Status, "failed", StringComparison.OrdinalIgnoreCase);

    public int ProgressPercent => TotalBytes > 0
        ? (int)Math.Clamp((double)ReceivedBytes / TotalBytes * 100.0, 0.0, 100.0)
        : (IsCompleted ? 100 : 0);

    public double ProgressRatio => TotalBytes > 0
        ? Math.Clamp((double)ReceivedBytes / TotalBytes, 0.0, 1.0)
        : (IsCompleted ? 1.0 : 0.0);

    public string PrimaryFileName => FileNames.Count > 0 ? FileNames[0] : "Incoming Transfer";

    public string DisplaySummary
    {
        get
        {
            if (FileNames.Count <= 1)
            {
                return $"{PrimaryFileName} ({FormattedTotalSize})";
            }
            return $"{FileNames.Count} files ({FormattedTotalSize})";
        }
    }

    public string FormattedTotalSize => FormatBytes(TotalBytes);
    public string FormattedReceivedSize => FormatBytes(ReceivedBytes);

    public string? FirstSavedPath => SavedPaths.Count > 0 ? SavedPaths[0] : null;

    private static string FormatBytes(long bytes)
    {
        if (bytes < 1024) return $"{bytes} B";
        if (bytes < 1024 * 1024) return $"{bytes / 1024.0:F1} KB";
        if (bytes < 1024 * 1024 * 1024) return $"{bytes / (1024.0 * 1024.0):F1} MB";
        return $"{bytes / (1024.0 * 1024.0 * 1024.0):F2} GB";
    }
}

public sealed class AirDropStatus
{
    [JsonPropertyName("enabled")]
    public bool Enabled { get; set; }

    [JsonPropertyName("computer_name")]
    public string ComputerName { get; set; } = "Relais PC";

    [JsonPropertyName("model_name")]
    public string ModelName { get; set; } = "MacBookPro18,3";

    [JsonPropertyName("port")]
    public int Port { get; set; } = 8771;

    [JsonPropertyName("sid")]
    public string Sid { get; set; } = "";

    [JsonPropertyName("downloads_dir")]
    public string DownloadsDir { get; set; } = "";

    [JsonPropertyName("active_transfers")]
    public List<AirDropTransferItem> ActiveTransfers { get; set; } = [];

    [JsonPropertyName("history")]
    public List<AirDropTransferItem> History { get; set; } = [];

    [JsonPropertyName("pending_count")]
    public int PendingCount { get; set; }
}
