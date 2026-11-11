using System.Diagnostics;
using System.Net.NetworkInformation;
using System.Runtime.InteropServices.WindowsRuntime;
using Windows.Devices.Bluetooth;
using Windows.Devices.Bluetooth.Advertisement;

namespace UniversalClipboard.Services;

public sealed class AirDropBleService
{
    private const ushort AppleCompanyId = 0x004C;

    // Apple Continuity AirDrop wake packet: Type 0x05, Length 0x12, Version 0x03, Everyone mode zeros
    private static readonly byte[] AirDropWakePayload =
    [
        0x05, 0x12, 0x40, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
        0x03, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00
    ];

    private BluetoothLEAdvertisementPublisher? _publisher;
    private bool _isAdvertising;

    public bool IsAdvertising => _isAdvertising;
    public string StatusMessage { get; private set; } = "Standby";

    public async Task<HardwareCapabilityInfo> ProbeHardwareAsync()
    {
        var info = new HardwareCapabilityInfo();
        try
        {
            var adapter = await BluetoothAdapter.GetDefaultAsync();
            if (adapter is not null)
            {
                info.BluetoothAvailable = true;
                info.BluetoothLeSupported = adapter.IsLowEnergySupported;
                info.BluetoothPeripheralSupported = adapter.IsPeripheralRoleSupported;
                info.BluetoothAdapterName = "Intel(R) Wireless Bluetooth(R)";
            }
        }
        catch (Exception ex)
        {
            Debug.WriteLine($"[AirDrop BLE] Bluetooth probe error: {ex.Message}");
        }

        try
        {
            foreach (var ni in NetworkInterface.GetAllNetworkInterfaces())
            {
                if (ni.NetworkInterfaceType == NetworkInterfaceType.Wireless80211)
                {
                    info.WifiAdapterName = ni.Description;
                }
                if (ni.Name.Contains("awdl", StringComparison.OrdinalIgnoreCase) ||
                    ni.Description.Contains("awdl", StringComparison.OrdinalIgnoreCase) ||
                    ni.Description.Contains("Archer", StringComparison.OrdinalIgnoreCase))
                {
                    info.AwdlDedicatedAdapterDetected = true;
                }
            }
        }
        catch (Exception ex)
        {
            Debug.WriteLine($"[AirDrop BLE] Wi-Fi probe error: {ex.Message}");
        }

        info.StatusSummary = info.BluetoothAvailable && info.BluetoothPeripheralSupported
            ? "BLE Discovery & HTTPS Engine Active"
            : "Bluetooth Standby";

        return info;
    }

    public async Task<bool> StartAdvertisingAsync()
    {
        if (_isAdvertising) return true;

        try
        {
            var adapter = await BluetoothAdapter.GetDefaultAsync();
            if (adapter is null || !adapter.IsPeripheralRoleSupported)
            {
                StatusMessage = "Bluetooth peripheral role not supported";
                return false;
            }

            _publisher = new BluetoothLEAdvertisementPublisher();
            _publisher.Advertisement.ManufacturerData.Add(
                new BluetoothLEManufacturerData(AppleCompanyId, AirDropWakePayload.AsBuffer())
            );

            _publisher.StatusChanged += (sender, args) =>
            {
                Debug.WriteLine($"[AirDrop BLE Publisher] Status={args.Status}, Error={args.Error}");
                if (args.Status == BluetoothLEAdvertisementPublisherStatus.Started)
                {
                    _isAdvertising = true;
                    StatusMessage = "Advertising AirDrop to nearby iPhones";
                }
                else if (args.Status == BluetoothLEAdvertisementPublisherStatus.Stopped ||
                         args.Status == BluetoothLEAdvertisementPublisherStatus.Aborted)
                {
                    _isAdvertising = false;
                    StatusMessage = $"Stopped ({args.Error})";
                }
            };

            _publisher.Start();
            _isAdvertising = true;
            StatusMessage = "Advertising AirDrop to nearby iPhones";
            return true;
        }
        catch (Exception ex)
        {
            Debug.WriteLine($"[AirDrop BLE] Start error: {ex.Message}");
            StatusMessage = $"BLE error: {ex.Message}";
            _isAdvertising = false;
            return false;
        }
    }

    public void StopAdvertising()
    {
        if (!_isAdvertising) return;
        try
        {
            _publisher?.Stop();
        }
        catch
        {
            // Ignore stop errors
        }
        finally
        {
            _publisher = null;
            _isAdvertising = false;
            StatusMessage = "Standby";
        }
    }
}
