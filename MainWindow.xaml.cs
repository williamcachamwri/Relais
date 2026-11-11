using System.Diagnostics;
using System.Numerics;
using System.Runtime.InteropServices;
using System.Text.Json;
using Microsoft.UI;
using Microsoft.UI.Windowing;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using Microsoft.UI.Xaml.Hosting;
using Microsoft.UI.Xaml.Input;
using Microsoft.UI.Xaml.Media;
using Microsoft.UI.Xaml.Media.Imaging;
using UniversalClipboard.Services;
using Windows.Graphics;
using Windows.System;
using Windows.UI;

namespace UniversalClipboard;

public sealed partial class MainWindow : Window
{
    private const int HotkeyIdWinAltV = 0x9001;
    private const int HotkeyIdCtrlShiftV = 0x9002;
    private const int WmHotkey = 0x0312;
    private const uint WmGetMinMaxInfo = 0x0024;
    private const int GwlpWndProc = -4;

    [StructLayout(LayoutKind.Sequential)]
    private struct POINT
    {
        public int x;
        public int y;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct MINMAXINFO
    {
        public POINT ptReserved;
        public POINT ptMaxSize;
        public POINT ptMaxPosition;
        public POINT ptMinTrackSize;
        public POINT ptMaxTrackSize;
    }
    private const uint ModAlt = 0x0001;
    private const uint ModControl = 0x0002;
    private const uint ModShift = 0x0004;
    private const uint ModWin = 0x0008;
    private const uint ModNoRepeat = 0x4000;
    private const uint VkV = 0x56;

    private delegate IntPtr WndProcDelegate(IntPtr hWnd, uint msg, IntPtr wParam, IntPtr lParam);

    private readonly ClipboardBackendService _backend = new();
    private readonly AirDropBleService _airDropBle = new();
    private readonly DispatcherTimer _pollTimer;
    private readonly QuickPasteWindow _quickPasteWindow;
    private IntPtr _hwnd;
    private IntPtr _origWndProc;
    private WndProcDelegate? _wndProcDelegate;

    private List<ClipboardHistoryItem> _allItems = [];
    private List<ClipboardHistoryItem> _filteredItems = [];
    private ClipboardHistoryItem? _selectedItem;
    private BackendStatus? _lastStatus;
    private AirDropStatus? _airDropStatus;
    private string? _currentPendingTransferId;
    private bool _isAirDropReceivingActive = true;
    private int _lastHistoryRevision = -1;

    private string _typeOrTagFilter = "type:all";
    private string _sourceFilter = "src:all";
    private string _timeFilter = "time:all";
    private string _searchQuery = "";
    private bool? _sidebarManualOverride;
    private bool _isDeveloperModeEnabled;
    private int _logPollCounter;
    private string _renderedItemIdsKey = "";

    // Carbon-Graphite Palette (Reference Image #2)
    private static readonly SolidColorBrush BrushTextPrimary = new(ColorHelper.FromArgb(0xFF, 0xF2, 0xF2, 0xF5));
    private static readonly SolidColorBrush BrushTextSecondary = new(ColorHelper.FromArgb(0xFF, 0x9A, 0x9A, 0xA2));
    private static readonly SolidColorBrush BrushTextMuted = new(ColorHelper.FromArgb(0xFF, 0x6E, 0x6E, 0x76));
    private static readonly SolidColorBrush BrushTextDim = new(ColorHelper.FromArgb(0xFF, 0x5E, 0x5E, 0x66));
    private static readonly SolidColorBrush BrushWhite = new(Colors.White);

    private static readonly SolidColorBrush BrushNeutralAccent = new(ColorHelper.FromArgb(0xFF, 0xDC, 0xDC, 0xE4));
    private static readonly SolidColorBrush BrushFileBadgeBg = new(ColorHelper.FromArgb(0xFF, 0x25, 0x25, 0x2C));
    private static readonly SolidColorBrush BrushLinkBlueText = new(ColorHelper.FromArgb(0xFF, 0x60, 0xA5, 0xFA));
    private static readonly SolidColorBrush BrushLinkBlueBg = new(ColorHelper.FromArgb(0xFF, 0x17, 0x2A, 0x3A));
    private static readonly SolidColorBrush BrushLinkBorder = new(ColorHelper.FromArgb(0xFF, 0x24, 0x48, 0x66));
    private static readonly SolidColorBrush BrushCodePurpleText = new(ColorHelper.FromArgb(0xFF, 0xA7, 0x8B, 0xFA));
    private static readonly SolidColorBrush BrushCodePurpleBg = new(ColorHelper.FromArgb(0xFF, 0x23, 0x1C, 0x35));
    private static readonly SolidColorBrush BrushCodeBorder = new(ColorHelper.FromArgb(0xFF, 0x3A, 0x2A, 0x56));
    private static readonly SolidColorBrush BrushTextBg = new(ColorHelper.FromArgb(0xFF, 0x22, 0x22, 0x2E));
    private static readonly SolidColorBrush BrushTextBorder = new(ColorHelper.FromArgb(0xFF, 0x32, 0x32, 0x42));
    private static readonly SolidColorBrush BrushStatusGreen = new(ColorHelper.FromArgb(0xFF, 0x34, 0xD3, 0x99));
    private static readonly SolidColorBrush BrushStatusAmber = new(ColorHelper.FromArgb(0xFF, 0xFE, 0xBC, 0x2E));

    private static readonly SolidColorBrush BrushSidebarActive = new(ColorHelper.FromArgb(0xFF, 0x28, 0x28, 0x2D));
    private static readonly SolidColorBrush BrushRailActive = new(ColorHelper.FromArgb(0xFF, 0x2A, 0x2A, 0x2E));
    private static readonly SolidColorBrush BrushRowSelected = new(ColorHelper.FromArgb(0xFF, 0x22, 0x22, 0x28));
    private static readonly SolidColorBrush BrushRowSelectedEdge = new(ColorHelper.FromArgb(0xFF, 0x2E, 0x2E, 0x36));
    private static readonly SolidColorBrush BrushRowHover = new(ColorHelper.FromArgb(0xFF, 0x1A, 0x1A, 0x1E));
    private static readonly SolidColorBrush BrushRowDivider = new(ColorHelper.FromArgb(0xFF, 0x22, 0x22, 0x25));
    private static readonly SolidColorBrush BrushThumbBorder = new(ColorHelper.FromArgb(0xFF, 0x2C, 0x2C, 0x30));
    private static readonly SolidColorBrush BrushThumbBg = new(ColorHelper.FromArgb(0xFF, 0x0E, 0x0E, 0x10));
    private static readonly SolidColorBrush BrushTransparent = new(Colors.Transparent);

    private readonly Dictionary<string, Border> _rowBorders = [];

    public MainWindow()
    {
        InitializeComponent();

        _quickPasteWindow = new QuickPasteWindow(_backend);

        ConfigureWindowChrome();
        RegisterGlobalHotkeys();
        AttachGlobalAnimations();
        SidebarPcNameText.Text = "This device";
        LoadAppSettings();

        RootGrid.Loaded += (_, _) => ApplyResponsiveLayout(RootGrid.ActualWidth > 0 ? RootGrid.ActualWidth : 1050, RootGrid.ActualHeight > 0 ? RootGrid.ActualHeight : 680);

        _pollTimer = new DispatcherTimer
        {
            Interval = TimeSpan.FromMilliseconds(650),
        };
        _pollTimer.Tick += OnPollTimerTick;

        Closed += OnWindowClosed;
        _ = InitializeBackendAsync();
        _ = InitializeAirDropAsync();
    }

    private void ConfigureWindowChrome()
    {
        ExtendsContentIntoTitleBar = true;
        SetTitleBar(TitleBarDragRegion);

        _hwnd = WinRT.Interop.WindowNative.GetWindowHandle(this);
        var windowId = Win32Interop.GetWindowIdFromWindow(_hwnd);
        var appWindow = AppWindow.GetFromWindowId(windowId);

        appWindow.Resize(new SizeInt32(1050, 680));

        if (appWindow.TitleBar is { } titleBar)
        {
            titleBar.ButtonBackgroundColor = Colors.Transparent;
            titleBar.ButtonInactiveBackgroundColor = Colors.Transparent;
            titleBar.ButtonForegroundColor = ColorHelper.FromArgb(0xFF, 0x8A, 0x8A, 0x92);
            titleBar.ButtonInactiveForegroundColor = ColorHelper.FromArgb(0xFF, 0x5A, 0x5A, 0x62);
            titleBar.ButtonHoverBackgroundColor = ColorHelper.FromArgb(0xFF, 0x23, 0x23, 0x28);
            titleBar.ButtonHoverForegroundColor = Colors.White;
        }
    }

    private void RegisterGlobalHotkeys()
    {
        if (_hwnd == IntPtr.Zero) return;

        _wndProcDelegate = CustomWndProc;
        var fnPtr = Marshal.GetFunctionPointerForDelegate(_wndProcDelegate);
        _origWndProc = SetWindowLongPtrW(_hwnd, GwlpWndProc, fnPtr);

        // Primary global shortcut: Win + Alt + V (like Windows + V)
        RegisterHotKey(_hwnd, HotkeyIdWinAltV, ModWin | ModAlt | ModNoRepeat, VkV);
        // Secondary fallback shortcut: Ctrl + Shift + V
        RegisterHotKey(_hwnd, HotkeyIdCtrlShiftV, ModControl | ModShift | ModNoRepeat, VkV);
    }

    private IntPtr CustomWndProc(IntPtr hWnd, uint msg, IntPtr wParam, IntPtr lParam)
    {
        if (msg == WmGetMinMaxInfo)
        {
            var dpi = GetDpiForWindow(hWnd);
            var scale = dpi > 0 ? (float)dpi / 96.0f : 1.0f;
            var mmi = Marshal.PtrToStructure<MINMAXINFO>(lParam);
            mmi.ptMinTrackSize.x = (int)(680 * scale);
            mmi.ptMinTrackSize.y = (int)(420 * scale);
            Marshal.StructureToPtr(mmi, lParam, false);
            return IntPtr.Zero;
        }

        if (msg == WmHotkey)
        {
            var id = wParam.ToInt32();
            if (id == HotkeyIdWinAltV || id == HotkeyIdCtrlShiftV)
            {
                var fgHwnd = GetForegroundWindow();
                DispatcherQueue.TryEnqueue(() => ToggleQuickPastePopup(fgHwnd));
                return IntPtr.Zero;
            }
        }
        return CallWindowProcW(_origWndProc, hWnd, msg, wParam, lParam);
    }

    private void ToggleQuickPastePopup(IntPtr fgHwnd)
    {
        if (_quickPasteWindow.IsPopupVisible)
        {
            _quickPasteWindow.HidePopup();
        }
        else
        {
            _quickPasteWindow.ShowPopup(fgHwnd, _allItems, _lastStatus);
        }
    }

    // -----------------------------------------------------------------------
    // Smooth Composition Animations (Apple-Design / Emil-Design-Eng)
    // -----------------------------------------------------------------------

    private void AttachGlobalAnimations()
    {
        var interactiveButtons = new UIElement[]
        {
            RailClipboardBtn, RailAirDropBtn, RailAboutBtn, RailSettingsBtn,
            NavTypeAllBtn, NavTypeFavBtn, NavTypeTextBtn, NavTypeRichBtn,
            NavTypeImageBtn, NavTypeFileBtn, NavTagLinkBtn, NavTagCodeBtn,
            NavTagImageBtn, NavSrcAllBtn, NavSrcPcBtn, NavSrcIphoneBtn,
            NavTimeAllBtn, NavTimeTodayBtn,
            ActionCopyBtn, ActionSendIphoneBtn, ActionFavoriteBtn, ActionDeleteBtn,
            PairWifiBtn, TransportAutoBtn, TransportWifiBtn, TransportUsbBtn,
        };

        foreach (var btn in interactiveButtons)
        {
            AttachPressAnimation(btn, 0.965f);
        }
    }

    // -----------------------------------------------------------------------
    // Responsive Window Layout Breakpoints
    // -----------------------------------------------------------------------

    private void OnRootGridSizeChanged(object sender, SizeChangedEventArgs e)
    {
        ApplyResponsiveLayout(e.NewSize.Width, e.NewSize.Height);
    }

    private void ApplyResponsiveLayout(double width, double height)
    {
        if (width <= 0 || height <= 0) return;

        // Auto-collapse sidebar when window is resized narrow (< 840px)
        if (width >= 920 && _sidebarManualOverride == true)
        {
            _sidebarManualOverride = null;
        }

        var showSidebar = _sidebarManualOverride ?? (width >= 840);
        if (width < 740)
        {
            showSidebar = false;
        }

        FilterSidebarBorder.Visibility = showSidebar ? Visibility.Visible : Visibility.Collapsed;
        ColSidebar.Width = showSidebar ? new GridLength(180) : new GridLength(0);

        // Keep Feed column completely stable and calm (330px default, 280px on very narrow window)
        ColFeed.Width = width < 660 ? new GridLength(280) : new GridLength(330);

        // Top Bar search box width and hint
        var showTopHint = width >= 820;
        TitleBarHintDot.Visibility = showTopHint ? Visibility.Visible : Visibility.Collapsed;
        TitleBarSyncHintText.Visibility = showTopHint ? Visibility.Visible : Visibility.Collapsed;
        SearchPillBorder.Width = width < 780 ? 190 : 270;

        // Remaining width is absorbed naturally by ColInspector (*)
        var remainingAfterCols = width - 50 - (showSidebar ? 180 : 0) - ColFeed.Width.Value;
        var compactBar = remainingAfterCols < 420;
        var veryCompactBar = remainingAfterCols < 330;

        InspectorBarPanel.Spacing = veryCompactBar ? 0 : (compactBar ? 2 : 4);
        ActionSendIphoneLabel.Text = compactBar ? "iPhone" : "Send to iPhone";

        var keyHintVis = veryCompactBar ? Visibility.Collapsed : Visibility.Visible;
        KeyHintC.Visibility = keyHintVis;
        KeyHintS.Visibility = keyHintVis;
        KeyHintF.Visibility = keyHintVis;
        KeyHintD.Visibility = keyHintVis;
    }

    private static void AttachPressAnimation(UIElement element, float pressedScale = 0.975f)
    {
        element.PointerPressed += (s, _) =>
        {
            if (s is FrameworkElement fe)
            {
                AnimateScale(fe, pressedScale, 90);
            }
        };
        element.PointerReleased += (s, _) =>
        {
            if (s is FrameworkElement fe)
            {
                AnimateScale(fe, 1.0f, 160);
            }
        };
        element.PointerExited += (s, _) =>
        {
            if (s is FrameworkElement fe)
            {
                AnimateScale(fe, 1.0f, 160);
            }
        };
    }

    private static void AnimateScale(FrameworkElement element, float targetScale, int durationMs)
    {
        try
        {
            var visual = ElementCompositionPreview.GetElementVisual(element);
            var w = (float)element.ActualWidth;
            var h = (float)element.ActualHeight;
            if (w > 0 && h > 0)
            {
                visual.CenterPoint = new Vector3(w / 2f, h / 2f, 0f);
            }

            var compositor = visual.Compositor;
            var easing = compositor.CreateCubicBezierEasingFunction(new Vector2(0.16f, 1.0f), new Vector2(0.3f, 1.0f));
            var anim = compositor.CreateVector3KeyFrameAnimation();
            anim.InsertKeyFrame(1.0f, new Vector3(targetScale, targetScale, 1.0f), easing);
            anim.Duration = TimeSpan.FromMilliseconds(durationMs);
            visual.StartAnimation("Scale", anim);
        }
        catch
        {
            // Ignore if visual tree not ready
        }
    }

    private static void AnimateStageEntrance(FrameworkElement element, int delayMs = 0)
    {
        try
        {
            var visual = ElementCompositionPreview.GetElementVisual(element);
            var compositor = visual.Compositor;
            var easing = compositor.CreateCubicBezierEasingFunction(new Vector2(0.16f, 1.0f), new Vector2(0.3f, 1.0f));

            var w = (float)element.ActualWidth;
            var h = (float)element.ActualHeight;
            if (w > 0 && h > 0)
            {
                visual.CenterPoint = new Vector3(w / 2f, h / 2f, 0f);
            }

            var fade = compositor.CreateScalarKeyFrameAnimation();
            fade.InsertKeyFrame(0.0f, 0.0f);
            fade.InsertKeyFrame(1.0f, 1.0f, easing);
            fade.Duration = TimeSpan.FromMilliseconds(260);
            if (delayMs > 0) fade.DelayTime = TimeSpan.FromMilliseconds(delayMs);

            var slide = compositor.CreateVector3KeyFrameAnimation();
            slide.InsertKeyFrame(0.0f, new Vector3(0f, 10f, 0f));
            slide.InsertKeyFrame(1.0f, Vector3.Zero, easing);
            slide.Duration = TimeSpan.FromMilliseconds(260);
            if (delayMs > 0) slide.DelayTime = TimeSpan.FromMilliseconds(delayMs);

            var scale = compositor.CreateVector3KeyFrameAnimation();
            scale.InsertKeyFrame(0.0f, new Vector3(0.985f, 0.985f, 1.0f));
            scale.InsertKeyFrame(1.0f, Vector3.One, easing);
            scale.Duration = TimeSpan.FromMilliseconds(260);
            if (delayMs > 0) scale.DelayTime = TimeSpan.FromMilliseconds(delayMs);

            visual.StartAnimation("Opacity", fade);
            visual.StartAnimation("Offset", slide);
            visual.StartAnimation("Scale", scale);
        }
        catch
        {
            // Ignore composition error
        }
    }

    private async Task InitializeBackendAsync()
    {
        await _backend.EnsureBackendRunningAsync();
        await RefreshStatusAndHistoryAsync();
        _pollTimer.Start();
    }

    private async void OnPollTimerTick(object? sender, object e)
    {
        try
        {
            await RefreshStatusAndHistoryAsync();
            await RefreshAirDropStatusAsync();
            if (LogsPane.Visibility == Visibility.Visible && ++_logPollCounter % 2 == 0)
            {
                await RefreshBackendLogsAsync();
            }
        }
        catch (Exception ex)
        {
            System.Diagnostics.Debug.WriteLine($"[OnPollTimerTick Error] {ex.Message}");
        }
    }

    private async Task RefreshStatusAndHistoryAsync()
    {
        var status = await _backend.GetStatusAsync();
        if (status is null)
        {
            InspectorPeerText.Text = "Peer offline";
            InspectorPeerIcon.Foreground = BrushTextMuted;
            return;
        }

        _lastStatus = status;
        UpdateStatusUi(status);

        if (status.HistoryRevision != _lastHistoryRevision)
        {
            var historyResp = await _backend.GetHistoryAsync();
            if (historyResp is not null)
            {
                _lastHistoryRevision = historyResp.Revision;
                _allItems = historyResp.Items;
                _quickPasteWindow.UpdateItemsIfVisible(_allItems);
                ApplyFiltersAndRenderFeed(animateRows: true);
            }
        }
    }

    private void UpdateStatusUi(BackendStatus status)
    {
        // Sidebar iPhone Name
        if (!string.IsNullOrWhiteSpace(status.DeviceName))
        {
            var paren = status.DeviceName.IndexOf('(');
            SidebarIphoneNameText.Text = paren > 0 ? status.DeviceName[..paren].Trim() : status.DeviceName;
        }

        // Inspector Top-Right Peer Status & Titlebar Hint
        if (status.Connected)
        {
            InspectorPeerText.Text = string.IsNullOrEmpty(status.ActiveTransport)
                ? "Peer online"
                : $"Peer online · {status.ActiveTransport}";
            InspectorPeerIcon.Glyph = "\uE701";
            InspectorPeerIcon.Foreground = BrushStatusGreen;
            TitleBarSyncHintText.Text = $"{SidebarIphoneNameText.Text} ⇄ Windows ({status.ActiveTransport})";
        }
        else
        {
            InspectorPeerText.Text = "Peer offline";
            InspectorPeerIcon.Glyph = "\uE701";
            InspectorPeerIcon.Foreground = BrushTextMuted;
            TitleBarSyncHintText.Text = "Searching for iPhone...";
        }

        // Onboarding pane status
        var step1Ready = status.UsbConnected || status.WifiPaired || status.Connected;
        Step1StatusText.Text = step1Ready ? "Trusted" : "Connect USB";
        Step1StatusText.Foreground = step1Ready ? BrushStatusGreen : BrushStatusAmber;
        if (step1Ready && !string.IsNullOrEmpty(status.Udid))
        {
            Step1DetailText.Text = $"{status.DeviceName} · iOS {status.IosVersion} ({status.Udid})";
        }

        Step2StatusText.Text = status.WifiPaired ? "Paired" : "Not paired";
        Step2StatusText.Foreground = status.WifiPaired ? BrushStatusGreen : BrushTextMuted;

        AboutDeviceText.Text = !string.IsNullOrWhiteSpace(status.DeviceName)
            ? $"{status.DeviceName} · iOS {status.IosVersion}"
            : "Searching for paired iPhone...";
        AboutTransportText.Text = status.Connected && !string.IsNullOrWhiteSpace(status.ActiveTransport)
            ? $"{status.ActiveTransport} (Connected)"
            : $"Standby ({status.TransportMode.ToUpperInvariant()})";

        UpdateTransportButtons(status.TransportMode);
    }

    private void UpdateTransportButtons(string mode)
    {
        mode = (mode ?? "auto").ToLowerInvariant();
        TransportAutoBtn.Background = mode == "auto" ? BrushSidebarActive : BrushTransparent;
        TransportAutoBtn.Foreground = mode == "auto" ? BrushTextPrimary : BrushTextSecondary;

        TransportWifiBtn.Background = mode == "wifi" ? BrushSidebarActive : BrushTransparent;
        TransportWifiBtn.Foreground = mode == "wifi" ? BrushTextPrimary : BrushTextSecondary;

        TransportUsbBtn.Background = mode == "usb" ? BrushSidebarActive : BrushTransparent;
        TransportUsbBtn.Foreground = mode == "usb" ? BrushTextPrimary : BrushTextSecondary;
    }

    private void ApplyFiltersAndRenderFeed(bool animateRows = false)
    {
        _filteredItems = _allItems.Where(item =>
        {
            var matchesTypeOrTag = _typeOrTagFilter switch
            {
                "type:favorites" => item.Pinned,
                "type:text" => !item.IsImage && !item.IsFile,
                "type:richtext" => item.IsRichText,
                "type:image" or "tag:image" => item.IsImage,
                "type:file" => item.IsFile || item.IsImage,
                "tag:link" => item.IsLink,
                "tag:code" => item.IsCode,
                _ => true,
            };
            if (!matchesTypeOrTag) return false;

            var matchesSource = _sourceFilter switch
            {
                "src:pc" => item.IsFromPc,
                "src:iphone" => !item.IsFromPc,
                _ => true,
            };
            if (!matchesSource) return false;

            var matchesTime = _timeFilter switch
            {
                "time:today" => item.IsToday,
                _ => true,
            };
            if (!matchesTime) return false;

            if (string.IsNullOrWhiteSpace(_searchQuery)) return true;
            var q = _searchQuery.Trim();
            return item.Text.Contains(q, StringComparison.OrdinalIgnoreCase)
                   || item.Uti.Contains(q, StringComparison.OrdinalIgnoreCase)
                   || item.FileDisplayName.Contains(q, StringComparison.OrdinalIgnoreCase)
                   || item.ImageDisplayName.Contains(q, StringComparison.OrdinalIgnoreCase);
        }).ToList();

        SearchItemCountText.Text = $"{_filteredItems.Count} items";
        EmptyFeedState.Visibility = _filteredItems.Count == 0 ? Visibility.Visible : Visibility.Collapsed;

        // Check if the filtered list IDs match the currently rendered list exactly to prevent flicker
        var newIdsKey = string.Join(",", _filteredItems.Select(x => x.Id + (x.Pinned ? ":p" : "")));
        if (newIdsKey == _renderedItemIdsKey && HistoryListPanel.Children.Count == _filteredItems.Count)
        {
            UpdateRowSelectionVisuals();
            return;
        }

        _renderedItemIdsKey = newIdsKey;
        HistoryListPanel.Children.Clear();
        _rowBorders.Clear();

        for (var i = 0; i < _filteredItems.Count; i++)
        {
            var item = _filteredItems[i];
            var row = BuildFeedRow(item);
            _rowBorders[item.Id] = row;
            HistoryListPanel.Children.Add(row);

            if (animateRows && i < 8)
            {
                AnimateStageEntrance(row, delayMs: i * 20);
            }
        }

        // Maintain selection or select newest item
        if (_selectedItem is not null)
        {
            var match = _filteredItems.FirstOrDefault(x => x.Id == _selectedItem.Id);
            SelectItem(match ?? _filteredItems.FirstOrDefault(), animateStage: false);
        }
        else
        {
            SelectItem(_filteredItems.FirstOrDefault(), animateStage: true);
        }
    }

    private Border BuildFeedRow(ClipboardHistoryItem item)
    {
        var isSelected = _selectedItem?.Id == item.Id;
        var rowBorder = new Border
        {
            Background = isSelected ? BrushRowSelected : BrushTransparent,
            BorderBrush = isSelected ? BrushRowSelectedEdge : BrushTransparent,
            BorderThickness = new Thickness(1),
            CornerRadius = new CornerRadius(8),
            Margin = new Thickness(6, 1, 6, 1),
            Padding = new Thickness(12, 10, 12, 10),
        };

        AttachPressAnimation(rowBorder, 0.99f);

        rowBorder.PointerEntered += (_, _) =>
        {
            if (_selectedItem?.Id != item.Id)
            {
                rowBorder.Background = BrushRowHover;
            }
        };
        rowBorder.PointerExited += (_, _) =>
        {
            if (_selectedItem?.Id != item.Id)
            {
                rowBorder.Background = BrushTransparent;
            }
        };
        rowBorder.PointerPressed += (_, _) =>
        {
            ShowInspectorPane();
            SelectItem(item, animateStage: _selectedItem?.Id != item.Id);
        };
        rowBorder.DoubleTapped += async (_, _) =>
        {
            SelectItem(item, animateStage: false);
            await CopySelectedItemAsync();
        };

        var rowStack = new StackPanel { Spacing = 6 };

        // 1. Top Metadata Line: [Icon] [Type] [Chars]  ...  [Device Icon] [Source · RelativeTime]
        var metaGrid = new Grid();
        metaGrid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
        metaGrid.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });

        var leftMeta = new StackPanel
        {
            Orientation = Orientation.Horizontal,
            Spacing = 6,
            VerticalAlignment = VerticalAlignment.Center,
        };

        if (item.IsImage)
        {
            leftMeta.Children.Add(new FontIcon
            {
                Glyph = "\uEB9F",
                FontSize = 11.5,
                Foreground = BrushTextSecondary,
            });
            leftMeta.Children.Add(new TextBlock
            {
                Text = "Image",
                FontSize = 12,
                FontWeight = Microsoft.UI.Text.FontWeights.Medium,
                Foreground = BrushTextSecondary,
            });
            if (item.Width > 0 && item.Height > 0)
            {
                var dimBlock = new TextBlock
                {
                    Text = $"{item.Width}×{item.Height}",
                    FontSize = 11.5,
                    Foreground = BrushTextDim,
                    Margin = new Thickness(4, 0, 0, 0),
                };
                Microsoft.UI.Xaml.Documents.Typography.SetNumeralAlignment(dimBlock, FontNumeralAlignment.Tabular);
                leftMeta.Children.Add(dimBlock);
            }
            else if (!string.IsNullOrWhiteSpace(item.FormattedSize))
            {
                var sizeBlock = new TextBlock
                {
                    Text = item.FormattedSize,
                    FontSize = 11.5,
                    Foreground = BrushTextDim,
                    Margin = new Thickness(4, 0, 0, 0),
                };
                Microsoft.UI.Xaml.Documents.Typography.SetNumeralAlignment(sizeBlock, FontNumeralAlignment.Tabular);
                leftMeta.Children.Add(sizeBlock);
            }
        }
        else if (item.IsFile)
        {
            leftMeta.Children.Add(new FontIcon
            {
                Glyph = "\uE7C3",
                FontSize = 11.5,
                Foreground = BrushTextSecondary,
            });
            leftMeta.Children.Add(new TextBlock
            {
                Text = "File",
                FontSize = 12,
                FontWeight = Microsoft.UI.Text.FontWeights.Medium,
                Foreground = BrushTextSecondary,
            });
            if (!string.IsNullOrWhiteSpace(item.FormattedSize))
            {
                var sizeBlock = new TextBlock
                {
                    Text = item.FormattedSize,
                    FontSize = 11.5,
                    Foreground = BrushTextDim,
                    Margin = new Thickness(4, 0, 0, 0),
                };
                Microsoft.UI.Xaml.Documents.Typography.SetNumeralAlignment(sizeBlock, FontNumeralAlignment.Tabular);
                leftMeta.Children.Add(sizeBlock);
            }
        }
        else
        {
            leftMeta.Children.Add(new FontIcon
            {
                Glyph = item.IsLink ? "\uE71B" : (item.IsCode ? "\uE943" : "\uE8A5"),
                FontSize = 11.5,
                Foreground = BrushTextSecondary,
            });
            leftMeta.Children.Add(new TextBlock
            {
                Text = item.IsLink ? "URL" : (item.IsCode ? "Code" : "Text"),
                FontSize = 12,
                FontWeight = Microsoft.UI.Text.FontWeights.Medium,
                Foreground = BrushTextSecondary,
            });
            var charsBlock = new TextBlock
            {
                Text = $"{item.Text.Length} chars",
                FontSize = 11.5,
                Foreground = BrushTextDim,
                Margin = new Thickness(4, 0, 0, 0),
            };
            Microsoft.UI.Xaml.Documents.Typography.SetNumeralAlignment(charsBlock, FontNumeralAlignment.Tabular);
            leftMeta.Children.Add(charsBlock);
        }

        if (item.Pinned)
        {
            leftMeta.Children.Add(new FontIcon
            {
                Glyph = "\uE735",
                FontSize = 11,
                Foreground = BrushNeutralAccent,
                Margin = new Thickness(4, 0, 0, 0),
            });
        }

        Grid.SetColumn(leftMeta, 0);
        metaGrid.Children.Add(leftMeta);

        var rightMeta = new StackPanel
        {
            Orientation = Orientation.Horizontal,
            Spacing = 5,
            VerticalAlignment = VerticalAlignment.Center,
        };
        rightMeta.Children.Add(new FontIcon
        {
            Glyph = item.IsFromPc ? "\uE7F8" : "\uE8EA",
            FontSize = 10.5,
            Foreground = BrushTextMuted,
        });
        var srcName = item.IsFromPc ? "This device" : item.ShortDeviceName;
        var srcTimeText = new TextBlock
        {
            Text = $"{srcName} · {item.RelativeTime}",
            FontSize = 11.5,
            Foreground = BrushTextMuted,
        };
        Microsoft.UI.Xaml.Documents.Typography.SetNumeralAlignment(srcTimeText, FontNumeralAlignment.Tabular);
        rightMeta.Children.Add(srcTimeText);

        Grid.SetColumn(rightMeta, 1);
        metaGrid.Children.Add(rightMeta);
        rowStack.Children.Add(metaGrid);

        // 2. Main Content Line (Thumbnail for Image, Tan Extension Badge for File, or Bold Text for Text/URL)
        if (item.IsImage)
        {
            var imgRow = new Grid { ColumnSpacing = 12, Margin = new Thickness(0, 2, 0, 0) };
            imgRow.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
            imgRow.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });

            var thumbBorder = new Border
            {
                Width = 42,
                Height = 42,
                CornerRadius = new CornerRadius(6),
                Background = BrushThumbBg,
                BorderBrush = BrushThumbBorder,
                BorderThickness = new Thickness(1),
            };

            try
            {
                thumbBorder.Child = new Image
                {
                    Source = new BitmapImage(new Uri(_backend.GetImageUrl(item.Id))),
                    Stretch = Stretch.UniformToFill,
                };
            }
            catch
            {
                // Ignore thumbnail load error
            }

            Grid.SetColumn(thumbBorder, 0);
            imgRow.Children.Add(thumbBorder);

            var titleBlock = new TextBlock
            {
                Text = item.ImageDisplayName,
                FontSize = 13.5,
                FontWeight = Microsoft.UI.Text.FontWeights.SemiBold,
                Foreground = BrushTextPrimary,
                VerticalAlignment = VerticalAlignment.Center,
                TextTrimming = TextTrimming.CharacterEllipsis,
                MaxLines = 1,
            };
            Grid.SetColumn(titleBlock, 1);
            imgRow.Children.Add(titleBlock);

            rowStack.Children.Add(imgRow);
        }
        else if (item.IsFile)
        {
            var fileRow = new Grid { ColumnSpacing = 12, Margin = new Thickness(0, 2, 0, 0) };
            fileRow.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
            fileRow.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });

            var badgeBorder = new Border
            {
                Width = 42,
                Height = 42,
                CornerRadius = new CornerRadius(8),
                Background = BrushFileBadgeBg,
                BorderBrush = BrushRowSelectedEdge,
                BorderThickness = new Thickness(1),
                Child = new TextBlock
                {
                    Text = item.FileExtensionBadge,
                    FontSize = 11,
                    FontWeight = Microsoft.UI.Text.FontWeights.Bold,
                    Foreground = BrushTextPrimary,
                    HorizontalAlignment = HorizontalAlignment.Center,
                    VerticalAlignment = VerticalAlignment.Center,
                },
            };

            Grid.SetColumn(badgeBorder, 0);
            fileRow.Children.Add(badgeBorder);

            var fileTitleBlock = new TextBlock
            {
                Text = item.FileDisplayName,
                FontSize = 13.5,
                FontWeight = Microsoft.UI.Text.FontWeights.SemiBold,
                Foreground = BrushTextPrimary,
                VerticalAlignment = VerticalAlignment.Center,
                TextWrapping = TextWrapping.Wrap,
                TextTrimming = TextTrimming.CharacterEllipsis,
                MaxLines = 2,
                LineHeight = 18,
            };
            Grid.SetColumn(fileTitleBlock, 1);
            fileRow.Children.Add(fileTitleBlock);

            rowStack.Children.Add(fileRow);
        }
        else
        {
            rowStack.Children.Add(new TextBlock
            {
                Text = item.Text.Replace("\r\n", "\n").Trim(),
                FontSize = 13.5,
                FontWeight = Microsoft.UI.Text.FontWeights.SemiBold,
                Foreground = BrushTextPrimary,
                MaxLines = 2,
                LineHeight = 19,
                TextWrapping = TextWrapping.Wrap,
                TextTrimming = TextTrimming.CharacterEllipsis,
            });

            // 3. Optional Subtle Tag Pill (Link or Code)
            if (item.IsLink)
            {
                rowStack.Children.Add(new Border
                {
                    Background = BrushLinkBlueBg,
                    CornerRadius = new CornerRadius(4),
                    Padding = new Thickness(6, 1, 6, 2),
                    HorizontalAlignment = HorizontalAlignment.Left,
                    Margin = new Thickness(0, 2, 0, 0),
                    Child = new TextBlock
                    {
                        Text = "Link",
                        FontSize = 11,
                        FontWeight = Microsoft.UI.Text.FontWeights.Medium,
                        Foreground = BrushLinkBlueText,
                    },
                });
            }
            else if (item.IsCode)
            {
                rowStack.Children.Add(new Border
                {
                    Background = BrushCodePurpleBg,
                    CornerRadius = new CornerRadius(4),
                    Padding = new Thickness(6, 1, 6, 2),
                    HorizontalAlignment = HorizontalAlignment.Left,
                    Margin = new Thickness(0, 2, 0, 0),
                    Child = new TextBlock
                    {
                        Text = "Code",
                        FontSize = 11,
                        FontWeight = Microsoft.UI.Text.FontWeights.Medium,
                        Foreground = BrushCodePurpleText,
                    },
                });
            }
        }

        rowBorder.Child = rowStack;
        return rowBorder;
    }

    private void SelectItem(ClipboardHistoryItem? item, bool animateStage = true)
    {
        var previousId = _selectedItem?.Id;
        _selectedItem = item;

        if (previousId is not null && _rowBorders.TryGetValue(previousId, out var prevBorder))
        {
            prevBorder.Background = BrushTransparent;
            prevBorder.BorderBrush = BrushTransparent;
        }

        if (item is not null && _rowBorders.TryGetValue(item.Id, out var newBorder))
        {
            newBorder.Background = BrushRowSelected;
            newBorder.BorderBrush = BrushRowSelectedEdge;
        }

        UpdateInspector(item, animateStage);
    }

    private void UpdateRowSelectionVisuals()
    {
        foreach (var (id, border) in _rowBorders)
        {
            var isSelected = _selectedItem?.Id == id;
            border.Background = isSelected ? BrushRowSelected : BrushTransparent;
            border.BorderBrush = isSelected ? BrushRowSelectedEdge : BrushTransparent;
        }
    }

    private void UpdateInspector(ClipboardHistoryItem? item, bool animateStage)
    {
        if (item is null)
        {
            InspectorMetaLeftText.Text = "No item selected";
            InspectorSourceIcon.Glyph = "\uE7F8";
            InspectorSourceText.Text = "This device";
            InspectorImageStage.Visibility = Visibility.Collapsed;
            InspectorFileStage.Visibility = Visibility.Collapsed;
            InspectorTextStage.Visibility = Visibility.Visible;
            InspectorOpenLinkBtn.Visibility = Visibility.Collapsed;
            InspectorTypeBadgeText.Text = "INFO";
            InspectorTypeBadgeIcon.Glyph = "\uE946";
            InspectorTypeBadge.Background = BrushTextBg;
            InspectorTypeBadge.BorderBrush = BrushTextBorder;
            InspectorTypeBadgeText.Foreground = BrushTextPrimary;
            InspectorTypeBadgeIcon.Foreground = BrushTextSecondary;
            InspectorCardHeaderTitle.Text = "Relais";
            InspectorPreviewText.Text = "Copy text, images, URLs, or files on your iPhone or Windows PC — both directions sync automatically.";
            InspectorChipsPanel.Visibility = Visibility.Collapsed;
            return;
        }

        InspectorChipsPanel.Visibility = Visibility.Visible;
        InspectorSourceIcon.Glyph = item.IsFromPc ? "\uE7F8" : "\uE8EA";
        InspectorSourceText.Text = item.IsFromPc ? "This device" : item.ShortDeviceName;
        ActionFavoriteIcon.Glyph = item.Pinned ? "\uE735" : "\uE734";
        ActionFavoriteIcon.Foreground = item.Pinned ? BrushNeutralAccent : BrushTextSecondary;
        ActionFavoriteLabel.Text = item.Pinned ? "Favorited" : "Favorite";

        var direction = item.IsFromPc ? "Windows → iPhone" : "iPhone → Windows";

        if (item.IsImage)
        {
            InspectorMetaLeftText.Text = item.Width > 0 && item.Height > 0
                ? $"Image  ·  {item.Width} × {item.Height} px"
                : (!string.IsNullOrWhiteSpace(item.FormattedSize) ? $"Image  ·  {item.FormattedSize}" : "Image");
            InspectorTextStage.Visibility = Visibility.Collapsed;
            InspectorFileStage.Visibility = Visibility.Collapsed;
            InspectorImageStage.Visibility = Visibility.Visible;

            try
            {
                InspectorPreviewImage.Source = new BitmapImage(new Uri(_backend.GetImageUrl(item.Id)));
            }
            catch
            {
                InspectorPreviewImage.Source = null;
            }

            InspectorImageMetaHeader.Text = item.Width > 0 && item.Height > 0
                ? $"{item.Width} × {item.Height} px  ·  {item.FormattedSize}"
                : item.FormattedSize;
            InspectorImageTitleText.Text = item.ImageDisplayName;
            InspectorImageDirText.Text = direction;
            InspectorImageUtiText.Text = item.Uti;
            InspectorImageTransportText.Text = $"{item.Transport}  ·  {item.LatencyMs:F1} ms";

            if (animateStage) AnimateStageEntrance(InspectorImageStage);
        }
        else if (item.IsFile)
        {
            InspectorMetaLeftText.Text = !string.IsNullOrWhiteSpace(item.FormattedSize)
                ? $"File  ·  {item.FormattedSize}"
                : "File";
            InspectorImageStage.Visibility = Visibility.Collapsed;
            InspectorTextStage.Visibility = Visibility.Collapsed;
            InspectorFileStage.Visibility = Visibility.Visible;

            InspectorFileExtText.Text = item.FileExtensionBadge;
            InspectorFileTitleText.Text = item.FileDisplayName;
            InspectorFileSubtitle.Text = $"{direction}  ·  {item.Uti}  ·  {item.FormattedSize}  ·  {item.Transport}  ·  {item.LatencyMs:F1} ms";
            if (animateStage) AnimateStageEntrance(InspectorFileStage);
        }
        else
        {
            InspectorImageStage.Visibility = Visibility.Collapsed;
            InspectorFileStage.Visibility = Visibility.Collapsed;
            InspectorTextStage.Visibility = Visibility.Visible;

            if (item.IsLink)
            {
                InspectorMetaLeftText.Text = $"URL  ·  {item.Text.Length} chars";
                InspectorTypeBadgeText.Text = "URL";
                InspectorTypeBadgeIcon.Glyph = "\uE71B";
                InspectorTypeBadge.Background = BrushLinkBlueBg;
                InspectorTypeBadge.BorderBrush = BrushLinkBorder;
                InspectorTypeBadgeText.Foreground = BrushLinkBlueText;
                InspectorTypeBadgeIcon.Foreground = BrushLinkBlueText;
                InspectorCardHeaderTitle.Text = item.ExtractedDomain ?? $"{item.Text.Length} characters";
                InspectorOpenLinkBtn.Visibility = item.ExtractedUrl != null ? Visibility.Visible : Visibility.Collapsed;
            }
            else if (item.IsCode)
            {
                InspectorMetaLeftText.Text = $"Code  ·  {item.Text.Length} chars";
                InspectorTypeBadgeText.Text = "CODE";
                InspectorTypeBadgeIcon.Glyph = "\uE943";
                InspectorTypeBadge.Background = BrushCodePurpleBg;
                InspectorTypeBadge.BorderBrush = BrushCodeBorder;
                InspectorTypeBadgeText.Foreground = BrushCodePurpleText;
                InspectorTypeBadgeIcon.Foreground = BrushCodePurpleText;
                InspectorCardHeaderTitle.Text = $"{item.Text.Length} characters";
                InspectorOpenLinkBtn.Visibility = Visibility.Collapsed;
            }
            else
            {
                InspectorMetaLeftText.Text = $"Text  ·  {item.Text.Length} chars";
                InspectorTypeBadgeText.Text = "TEXT";
                InspectorTypeBadgeIcon.Glyph = "\uE8A5";
                InspectorTypeBadge.Background = BrushTextBg;
                InspectorTypeBadge.BorderBrush = BrushTextBorder;
                InspectorTypeBadgeText.Foreground = BrushTextPrimary;
                InspectorTypeBadgeIcon.Foreground = BrushTextSecondary;
                InspectorCardHeaderTitle.Text = $"{item.Text.Length} characters";
                InspectorOpenLinkBtn.Visibility = Visibility.Collapsed;
            }

            InspectorPreviewText.Text = item.Text;
            InspectorPreviewText.FontFamily = item.IsCode
                ? new FontFamily("Cascadia Code, Consolas")
                : new FontFamily("Segoe UI Variable Text, Segoe UI");
            InspectorPreviewText.FontSize = item.IsCode ? 13 : 13.5;
            InspectorTextScrollViewer?.ChangeView(0, 0, 1.0f, true);

            InspectorDirectionText.Text = direction;
            InspectorDirectionIcon.Glyph = item.IsFromPc ? "\uE7F8" : "\uE8EA";
            InspectorFormatText.Text = item.Uti;
            InspectorTransportText.Text = item.Transport;
            InspectorLatencyText.Text = $"{item.FormattedSize}  ·  {item.LatencyMs:F1} ms";

            if (animateStage) AnimateStageEntrance(InspectorTextStage);
        }
    }

    private async void OnInspectorOpenLinkClick(object sender, RoutedEventArgs e)
    {
        try
        {
            var url = _selectedItem?.ExtractedUrl;
            if (!string.IsNullOrEmpty(url) && Uri.TryCreate(url, UriKind.Absolute, out var uri))
            {
                await Windows.System.Launcher.LaunchUriAsync(uri);
            }
        }
        catch (Exception ex)
        {
            ShowNotification("Không thể mở liên kết: " + ex.Message, InfoBarSeverity.Warning);
        }
    }

    private async Task CopySelectedItemAsync()
    {
        if (_selectedItem is null) return;
        var ok = await _backend.CopyItemToWindowsAsync(_selectedItem.Id);
        if (ok)
        {
            ActionCopyLabel.Text = "Copied ✓";
            ActionCopyLabel.Foreground = BrushStatusGreen;
            await Task.Delay(1100);
            ActionCopyLabel.Text = "Copy";
            ActionCopyLabel.Foreground = BrushTextPrimary;
        }
    }

    private async Task SendSelectedItemToIphoneAsync()
    {
        if (_selectedItem is null) return;
        ActionSendIphoneLabel.Text = "Sending...";
        var ok = await _backend.SendItemToIphoneAsync(_selectedItem.Id);
        if (ok)
        {
            ActionSendIphoneLabel.Text = "Sent to iPhone ✓";
            ActionSendIphoneLabel.Foreground = BrushStatusGreen;
            await Task.Delay(1200);
        }
        else
        {
            ActionSendIphoneLabel.Text = "Send failed";
            ActionSendIphoneLabel.Foreground = BrushStatusAmber;
            await Task.Delay(1200);
        }
        ActionSendIphoneLabel.Text = "Send to iPhone";
        ActionSendIphoneLabel.Foreground = BrushTextSecondary;
    }

    private void ShowInspectorPane()
    {
        if (InspectorPane.Visibility != Visibility.Visible)
        {
            OnboardingPane.Visibility = Visibility.Collapsed;
            AboutPane.Visibility = Visibility.Collapsed;
            LogsPane.Visibility = Visibility.Collapsed;
            AirDropPane.Visibility = Visibility.Collapsed;
            InspectorPane.Visibility = Visibility.Visible;
            RailClipboardBtn.Background = BrushRailActive;
            RailAboutBtn.Background = BrushTransparent;
            RailSettingsBtn.Background = BrushTransparent;
            RailLogsBtn.Background = BrushTransparent;
            RailAirDropBtn.Background = BrushTransparent;
            AnimateStageEntrance(InspectorPane);
        }
    }

    private void ShowOnboardingPane()
    {
        InspectorPane.Visibility = Visibility.Collapsed;
        AboutPane.Visibility = Visibility.Collapsed;
        LogsPane.Visibility = Visibility.Collapsed;
        AirDropPane.Visibility = Visibility.Collapsed;
        OnboardingPane.Visibility = Visibility.Visible;
        RailSettingsBtn.Background = BrushRailActive;
        RailAboutBtn.Background = BrushTransparent;
        RailClipboardBtn.Background = BrushTransparent;
        RailLogsBtn.Background = BrushTransparent;
        RailAirDropBtn.Background = BrushTransparent;
        AnimateStageEntrance(OnboardingPane);
    }

    private void ShowAboutPane()
    {
        InspectorPane.Visibility = Visibility.Collapsed;
        OnboardingPane.Visibility = Visibility.Collapsed;
        LogsPane.Visibility = Visibility.Collapsed;
        AirDropPane.Visibility = Visibility.Collapsed;
        AboutPane.Visibility = Visibility.Visible;
        RailAboutBtn.Background = BrushRailActive;
        RailSettingsBtn.Background = BrushTransparent;
        RailClipboardBtn.Background = BrushTransparent;
        RailLogsBtn.Background = BrushTransparent;
        RailAirDropBtn.Background = BrushTransparent;
        AnimateStageEntrance(AboutPane);
    }

    private void ShowLogsPane()
    {
        InspectorPane.Visibility = Visibility.Collapsed;
        OnboardingPane.Visibility = Visibility.Collapsed;
        AboutPane.Visibility = Visibility.Collapsed;
        AirDropPane.Visibility = Visibility.Collapsed;
        LogsPane.Visibility = Visibility.Visible;
        RailLogsBtn.Background = BrushRailActive;
        RailAboutBtn.Background = BrushTransparent;
        RailSettingsBtn.Background = BrushTransparent;
        RailClipboardBtn.Background = BrushTransparent;
        RailAirDropBtn.Background = BrushTransparent;
        AnimateStageEntrance(LogsPane);
        _ = RefreshBackendLogsAsync();
    }

    private void ShowAirDropPane()
    {
        InspectorPane.Visibility = Visibility.Collapsed;
        OnboardingPane.Visibility = Visibility.Collapsed;
        AboutPane.Visibility = Visibility.Collapsed;
        LogsPane.Visibility = Visibility.Collapsed;
        AirDropPane.Visibility = Visibility.Visible;
        RailAirDropBtn.Background = BrushRailActive;
        RailClipboardBtn.Background = BrushTransparent;
        RailAboutBtn.Background = BrushTransparent;
        RailSettingsBtn.Background = BrushTransparent;
        RailLogsBtn.Background = BrushTransparent;
        AnimateStageEntrance(AirDropPane);
        _ = RefreshAirDropStatusAsync();
    }

    private void OnRailAirDropClick(object sender, RoutedEventArgs e)
    {
        if (AirDropPane.Visibility == Visibility.Visible)
        {
            ShowInspectorPane();
        }
        else
        {
            ShowAirDropPane();
        }
    }

    private void OnRailLogsClick(object sender, RoutedEventArgs e)
    {
        if (LogsPane.Visibility == Visibility.Visible)
        {
            ShowInspectorPane();
        }
        else
        {
            ShowLogsPane();
        }
    }

    private void OnDeveloperModeToggled(object sender, RoutedEventArgs e)
    {
        _isDeveloperModeEnabled = DeveloperModeToggle.IsOn;
        RailLogsBtn.Visibility = _isDeveloperModeEnabled ? Visibility.Visible : Visibility.Collapsed;
        SaveAppSettings();
        if (!_isDeveloperModeEnabled && LogsPane.Visibility == Visibility.Visible)
        {
            ShowInspectorPane();
        }
    }

    private async Task RefreshBackendLogsAsync()
    {
        try
        {
            var logs = await _backend.GetBackendLogsAsync(350);
            var text = logs.Count > 0 ? string.Join(Environment.NewLine, logs) : "(No logs captured yet from CoreDevice daemon)";
            LogsContentTextBox.Text = text;
            LogsFooterInfo.Text = $"Showing {logs.Count} lines · 127.0.0.1:8767";
            if (LogsAutoScrollCheck.IsChecked == true)
            {
                LogsScrollViewer.ChangeView(null, LogsScrollViewer.ScrollableHeight, null, false);
            }
        }
        catch (Exception ex)
        {
            LogsContentTextBox.Text = $"Failed to fetch logs: {ex.Message}";
        }
    }

    private DispatcherTimer? _infoBarTimer;

    public void ShowNotification(string message, InfoBarSeverity severity = InfoBarSeverity.Informational, string? title = null, int autoDismissMs = 4500)
    {
        try
        {
            GlobalNotificationInfoBar.Title = title ?? (severity switch
            {
                InfoBarSeverity.Error => "Lỗi",
                InfoBarSeverity.Warning => "Cảnh báo",
                InfoBarSeverity.Success => "Thành công",
                _ => "Thông báo"
            });
            GlobalNotificationInfoBar.Message = message;
            GlobalNotificationInfoBar.Severity = severity;
            GlobalNotificationInfoBar.IsOpen = true;

            _infoBarTimer?.Stop();
            if (autoDismissMs > 0)
            {
                _infoBarTimer = new DispatcherTimer { Interval = TimeSpan.FromMilliseconds(autoDismissMs) };
                _infoBarTimer.Tick += (_, _) =>
                {
                    _infoBarTimer.Stop();
                    GlobalNotificationInfoBar.IsOpen = false;
                };
                _infoBarTimer.Start();
            }
        }
        catch
        {
            // Ignore
        }
    }

    private async void OnRefreshLogsClick(object sender, RoutedEventArgs e)
    {
        try
        {
            await RefreshBackendLogsAsync();
        }
        catch (Exception ex)
        {
            ShowNotification("Không thể làm mới log: " + ex.Message, InfoBarSeverity.Warning);
        }
    }

    private async void OnClearLogsClick(object sender, RoutedEventArgs e)
    {
        try
        {
            await _backend.ClearBackendLogsAsync();
            await RefreshBackendLogsAsync();
        }
        catch (Exception ex)
        {
            ShowNotification("Không thể xóa log backend: " + ex.Message, InfoBarSeverity.Warning);
        }
    }

    private void OnCopyLogsClick(object sender, RoutedEventArgs e)
    {
        try
        {
            var dp = new Windows.ApplicationModel.DataTransfer.DataPackage();
            dp.SetText(LogsContentTextBox.Text);
            Windows.ApplicationModel.DataTransfer.Clipboard.SetContent(dp);
            LogsFooterInfo.Text = "Copied all logs to Windows Clipboard!";
        }
        catch
        {
            // Ignore
        }
    }

    private static string GetSettingsFilePath()
    {
        var dir = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "UniversalClipboard");
        Directory.CreateDirectory(dir);
        return Path.Combine(dir, "app_settings.json");
    }

    private void LoadAppSettings()
    {
        try
        {
            var file = GetSettingsFilePath();
            if (File.Exists(file))
            {
                var json = File.ReadAllText(file);
                using var doc = JsonDocument.Parse(json);
                if (doc.RootElement.TryGetProperty("developer_mode", out var devProp))
                {
                    _isDeveloperModeEnabled = devProp.GetBoolean();
                }
                if (doc.RootElement.TryGetProperty("airdrop_enabled", out var adProp))
                {
                    _isAirDropReceivingActive = adProp.GetBoolean();
                }
            }
        }
        catch
        {
            _isDeveloperModeEnabled = false;
            _isAirDropReceivingActive = true;
        }

        DeveloperModeToggle.IsOn = _isDeveloperModeEnabled;
        RailLogsBtn.Visibility = _isDeveloperModeEnabled ? Visibility.Visible : Visibility.Collapsed;
        AirDropMainToggle.IsOn = _isAirDropReceivingActive;
        AirDropQuickToggle.IsOn = _isAirDropReceivingActive;
    }

    private void SaveAppSettings()
    {
        try
        {
            var file = GetSettingsFilePath();
            var json = JsonSerializer.Serialize(new
            {
                developer_mode = _isDeveloperModeEnabled,
                airdrop_enabled = _isAirDropReceivingActive,
            });
            File.WriteAllText(file, json);
        }
        catch
        {
            // Ignore
        }
    }

    private async Task InitializeAirDropAsync()
    {
        try
        {
            var hw = await _airDropBle.ProbeHardwareAsync();
            AirDropBleStatusChip.Text = hw.BluetoothAvailable && hw.BluetoothPeripheralSupported ? "Continuity Wake Active" : "Standby";
            AirDropWifiStatusChip.Text = string.IsNullOrEmpty(hw.WifiAdapterName) ? "Intel AX211" : hw.WifiAdapterName;
            AirDropProtoStatusChip.Text = "mDNS & HTTPS";
            AirDropAwdlNote.Text = hw.AwdlGuidanceMessage;

            if (_isAirDropReceivingActive)
            {
                await _airDropBle.StartAdvertisingAsync();
                await _backend.ToggleAirDropAsync(true);
            }
            await RefreshAirDropStatusAsync();
        }
        catch (Exception ex)
        {
            System.Diagnostics.Debug.WriteLine($"[InitializeAirDropAsync Error] {ex.Message}");
        }
    }

    private async Task RefreshAirDropStatusAsync()
    {
        try
        {
            var airdrop = await _backend.GetAirDropStatusAsync();
            if (airdrop == null) return;
            _airDropStatus = airdrop;

            // Update Header Status Pill
            if (airdrop.Enabled)
            {
                AirDropStatusPillText.Text = "● Ready · Everyone";
                AirDropStatusPillText.Foreground = BrushStatusGreen;
                AirDropStatusPill.Background = new SolidColorBrush(ColorHelper.FromArgb(0xFF, 0x1C, 0x26, 0x1C));
                AirDropStatusPill.BorderBrush = new SolidColorBrush(ColorHelper.FromArgb(0xFF, 0x2D, 0x48, 0x2D));
            }
            else
            {
                AirDropStatusPillText.Text = "Standby";
                AirDropStatusPillText.Foreground = BrushTextMuted;
                AirDropStatusPill.Background = new SolidColorBrush(ColorHelper.FromArgb(0xFF, 0x22, 0x22, 0x26));
                AirDropStatusPill.BorderBrush = new SolidColorBrush(ColorHelper.FromArgb(0xFF, 0x2C, 0x2C, 0x32));
            }

            // Sync Toggles if out of sync
            if (AirDropMainToggle.IsOn != airdrop.Enabled)
            {
                AirDropMainToggle.IsOn = airdrop.Enabled;
            }
            if (AirDropQuickToggle.IsOn != airdrop.Enabled)
            {
                AirDropQuickToggle.IsOn = airdrop.Enabled;
            }
            if (!string.IsNullOrEmpty(airdrop.ComputerName) && string.IsNullOrEmpty(AirDropDeviceNameInput.Text))
            {
                AirDropDeviceNameInput.Text = airdrop.ComputerName;
            }

            // Check for Pending Transfer Consent Request
            var pending = airdrop.ActiveTransfers.FirstOrDefault(t => t.IsPendingConsent);
            if (pending != null)
            {
                _currentPendingTransferId = pending.Id;
                AirDropIncomingSenderText.Text = pending.SenderName;
                AirDropIncomingFilesText.Text = $"wants to share {pending.DisplaySummary}";
                AirDropIncomingPromptCard.Visibility = Visibility.Visible;
            }
            else
            {
                AirDropIncomingPromptCard.Visibility = Visibility.Collapsed;
            }

            // Check for In-Progress Transfer
            var transferring = airdrop.ActiveTransfers.FirstOrDefault(t => t.IsTransferring);
            if (transferring != null)
            {
                AirDropActiveTransferCard.Visibility = Visibility.Visible;
                AirDropActiveTransferTitle.Text = $"Receiving from {transferring.SenderName}...";
                AirDropActiveTransferSub.Text = transferring.DisplaySummary;
                AirDropActiveProgressBar.Value = transferring.ProgressPercent;
                AirDropActiveTransferPercent.Text = $"{transferring.ProgressPercent}%";
            }
            else
            {
                AirDropActiveTransferCard.Visibility = Visibility.Collapsed;
            }

            // Refresh recent transfers if AirDrop pane is visible
            if (AirDropPane.Visibility == Visibility.Visible)
            {
                AirDropTransferCountText.Text = $"{airdrop.History.Count} transfers";
                RenderAirDropHistory(airdrop.History);
            }
        }
        catch (Exception ex)
        {
            System.Diagnostics.Debug.WriteLine($"[RefreshAirDropStatusAsync Error] {ex.Message}");
        }
    }

    private void RenderAirDropHistory(List<AirDropTransferItem> history)
    {
        AirDropRecentTransfersPanel.Children.Clear();
        if (history.Count == 0)
        {
            AirDropRecentTransfersPanel.Children.Add(new TextBlock
            {
                Text = "No AirDrop transfers received yet. Send a photo or file from iPhone via AirDrop to see it here.",
                FontSize = 12,
                Foreground = BrushTextMuted,
                Padding = new Thickness(4, 8, 4, 8),
            });
            return;
        }

        foreach (var item in history)
        {
            var card = new Border
            {
                Background = new SolidColorBrush(ColorHelper.FromArgb(0xFF, 0x14, 0x14, 0x17)),
                BorderBrush = new SolidColorBrush(ColorHelper.FromArgb(0xFF, 0x24, 0x24, 0x2A)),
                BorderThickness = new Thickness(1),
                CornerRadius = new CornerRadius(8),
                Padding = new Thickness(12, 10, 12, 10),
            };

            var grid = new Grid { ColumnSpacing = 12 };
            grid.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
            grid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
            grid.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });

            // Icon
            var iconBorder = new Border
            {
                Width = 32,
                Height = 32,
                CornerRadius = new CornerRadius(6),
                Background = new SolidColorBrush(ColorHelper.FromArgb(0xFF, 0x1E, 0x1E, 0x24)),
                VerticalAlignment = VerticalAlignment.Center,
            };
            var glyph = item.PrimaryFileName.EndsWith(".heic", StringComparison.OrdinalIgnoreCase)
                     || item.PrimaryFileName.EndsWith(".jpg", StringComparison.OrdinalIgnoreCase)
                     || item.PrimaryFileName.EndsWith(".png", StringComparison.OrdinalIgnoreCase)
                     || item.PrimaryFileName.EndsWith(".mov", StringComparison.OrdinalIgnoreCase)
                     || item.PrimaryFileName.EndsWith(".mp4", StringComparison.OrdinalIgnoreCase)
                     ? "\uEB9F" : "\uE8A5";
            iconBorder.Child = new FontIcon
            {
                Glyph = glyph,
                FontSize = 14,
                Foreground = BrushTextSecondary,
                HorizontalAlignment = HorizontalAlignment.Center,
                VerticalAlignment = VerticalAlignment.Center,
            };
            Grid.SetColumn(iconBorder, 0);
            grid.Children.Add(iconBorder);

            // Metadata text
            var textStack = new StackPanel { Spacing = 2, VerticalAlignment = VerticalAlignment.Center };
            var titleBlock = new TextBlock
            {
                Text = item.DisplaySummary,
                FontSize = 12.5,
                FontWeight = Microsoft.UI.Text.FontWeights.SemiBold,
                Foreground = BrushTextPrimary,
                TextTrimming = TextTrimming.CharacterEllipsis,
            };
            var subText = item.IsCompleted
                ? $"From {item.SenderName} · {item.TimeLabel}"
                : $"From {item.SenderName} · {item.Status}";
            var subBlock = new TextBlock
            {
                Text = subText,
                FontSize = 11,
                Foreground = item.IsCompleted ? BrushTextMuted : (item.IsFailed ? new SolidColorBrush(Colors.IndianRed) : BrushTextDim),
            };
            textStack.Children.Add(titleBlock);
            textStack.Children.Add(subBlock);
            Grid.SetColumn(textStack, 1);
            grid.Children.Add(textStack);

            // Action button (Open file if completed)
            if (item.IsCompleted && item.FirstSavedPath != null && File.Exists(item.FirstSavedPath))
            {
                var openBtn = new Button
                {
                    Background = new SolidColorBrush(ColorHelper.FromArgb(0xFF, 0x20, 0x20, 0x26)),
                    BorderBrush = new SolidColorBrush(ColorHelper.FromArgb(0xFF, 0x2C, 0x2C, 0x32)),
                    BorderThickness = new Thickness(1),
                    CornerRadius = new CornerRadius(6),
                    Padding = new Thickness(10, 4, 10, 4),
                    VerticalAlignment = VerticalAlignment.Center,
                    Content = new TextBlock { Text = "Open", FontSize = 11.5, Foreground = BrushTextPrimary },
                };
                var path = item.FirstSavedPath;
                openBtn.Click += (_, _) =>
                {
                    try
                    {
                        Process.Start(new ProcessStartInfo { FileName = path, UseShellExecute = true });
                    }
                    catch (Exception ex)
                    {
                        ShowNotification($"Could not open file: {ex.Message}", InfoBarSeverity.Error);
                    }
                };
                Grid.SetColumn(openBtn, 2);
                grid.Children.Add(openBtn);
            }
            else
            {
                var statusBadge = new Border
                {
                    Background = item.IsCompleted
                        ? new SolidColorBrush(ColorHelper.FromArgb(0xFF, 0x1A, 0x26, 0x1A))
                        : new SolidColorBrush(ColorHelper.FromArgb(0xFF, 0x22, 0x22, 0x26)),
                    CornerRadius = new CornerRadius(4),
                    Padding = new Thickness(6, 2, 6, 2),
                    VerticalAlignment = VerticalAlignment.Center,
                    Child = new TextBlock
                    {
                        Text = item.Status.ToUpperInvariant(),
                        FontSize = 10,
                        FontWeight = Microsoft.UI.Text.FontWeights.SemiBold,
                        Foreground = item.IsCompleted ? BrushStatusGreen : BrushTextDim,
                    },
                };
                Grid.SetColumn(statusBadge, 2);
                grid.Children.Add(statusBadge);
            }

            card.Child = grid;
            AirDropRecentTransfersPanel.Children.Add(card);
        }
    }

    private async void OnAirDropAcceptClick(object sender, RoutedEventArgs e)
    {
        if (string.IsNullOrEmpty(_currentPendingTransferId)) return;
        var id = _currentPendingTransferId;
        AirDropIncomingPromptCard.Visibility = Visibility.Collapsed;
        var ok = await _backend.SubmitAirDropConsentAsync(id, accept: true);
        if (ok)
        {
            ShowNotification("AirDrop transfer accepted. Receiving files...", InfoBarSeverity.Informational, "AirDrop");
        }
        await RefreshAirDropStatusAsync();
    }

    private async void OnAirDropDeclineClick(object sender, RoutedEventArgs e)
    {
        if (string.IsNullOrEmpty(_currentPendingTransferId)) return;
        var id = _currentPendingTransferId;
        AirDropIncomingPromptCard.Visibility = Visibility.Collapsed;
        await _backend.SubmitAirDropConsentAsync(id, accept: false);
        ShowNotification("AirDrop transfer declined.", InfoBarSeverity.Informational, "AirDrop");
        await RefreshAirDropStatusAsync();
    }

    private async void OnAirDropReceivingToggled(object sender, RoutedEventArgs e)
    {
        var isOn = (sender is ToggleSwitch ts) ? ts.IsOn : AirDropMainToggle.IsOn;
        _isAirDropReceivingActive = isOn;
        if (AirDropMainToggle.IsOn != isOn) AirDropMainToggle.IsOn = isOn;
        if (AirDropQuickToggle.IsOn != isOn) AirDropQuickToggle.IsOn = isOn;

        await _backend.ToggleAirDropAsync(isOn);
        if (isOn)
        {
            await _airDropBle.StartAdvertisingAsync();
            ShowNotification("AirDrop receiving enabled. Nearby Apple devices can discover this PC.", InfoBarSeverity.Success, "AirDrop");
        }
        else
        {
            _airDropBle.StopAdvertising();
            ShowNotification("AirDrop receiving turned off.", InfoBarSeverity.Informational, "AirDrop");
        }
        SaveAppSettings();
        await RefreshAirDropStatusAsync();
    }

    private async void OnSaveAirDropDeviceNameClick(object sender, RoutedEventArgs e)
    {
        var name = AirDropDeviceNameInput.Text.Trim();
        if (!string.IsNullOrEmpty(name))
        {
            await _backend.ConfigureAirDropAsync(computerName: name);
            ShowNotification($"AirDrop device name set to '{name}'.", InfoBarSeverity.Success, "AirDrop");
            await RefreshAirDropStatusAsync();
        }
    }

    private void OnOpenAirDropFolderClick(object sender, RoutedEventArgs e)
    {
        try
        {
            var downloadsDir = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.UserProfile), "Downloads", "AirDrop");
            Directory.CreateDirectory(downloadsDir);
            Process.Start(new ProcessStartInfo
            {
                FileName = downloadsDir,
                UseShellExecute = true,
            });
        }
        catch (Exception ex)
        {
            ShowNotification($"Could not open AirDrop folder: {ex.Message}", InfoBarSeverity.Error);
        }
    }

    private void OnRailClipboardClick(object sender, RoutedEventArgs e)
    {
        if (InspectorPane.Visibility != Visibility.Visible)
        {
            ShowInspectorPane();
            return;
        }

        // When window is narrow (< 920px), clicking the Clipboard Rail icon toggles the Filter Sidebar
        if (RootGrid.ActualWidth < 920)
        {
            var currentlyVisible = FilterSidebarBorder.Visibility == Visibility.Visible && ColSidebar.Width.Value > 0;
            _sidebarManualOverride = !currentlyVisible;
            ApplyResponsiveLayout(RootGrid.ActualWidth, RootGrid.ActualHeight);
        }
    }

    private void OnRailAboutClick(object sender, RoutedEventArgs e)
    {
        if (AboutPane.Visibility == Visibility.Visible)
        {
            ShowInspectorPane();
        }
        else
        {
            ShowAboutPane();
        }
    }

    private void OnRailDeviceClick(object sender, RoutedEventArgs e)
    {
        if (OnboardingPane.Visibility == Visibility.Visible)
        {
            ShowInspectorPane();
        }
        else
        {
            ShowOnboardingPane();
        }
    }

    private void OnSidebarFilterClick(object sender, RoutedEventArgs e)
    {
        if (sender is not Button clicked || clicked.Tag is not string tag) return;
        _typeOrTagFilter = tag;

        var buttons = new[]
        {
            NavTypeAllBtn, NavTypeFavBtn, NavTypeTextBtn, NavTypeRichBtn,
            NavTypeImageBtn, NavTypeFileBtn, NavTagLinkBtn, NavTagCodeBtn, NavTagImageBtn
        };
        UpdateSidebarButtonGroup(buttons, clicked);
        ShowInspectorPane();
        ApplyFiltersAndRenderFeed(animateRows: true);
    }

    private void OnSourceFilterClick(object sender, RoutedEventArgs e)
    {
        if (sender is not Button clicked || clicked.Tag is not string tag) return;
        _sourceFilter = tag;

        var buttons = new[] { NavSrcAllBtn, NavSrcPcBtn, NavSrcIphoneBtn };
        UpdateSidebarButtonGroup(buttons, clicked);
        ApplyFiltersAndRenderFeed(animateRows: true);
    }

    private void OnTimeFilterClick(object sender, RoutedEventArgs e)
    {
        if (sender is not Button clicked || clicked.Tag is not string tag) return;
        _timeFilter = tag;

        var buttons = new[] { NavTimeAllBtn, NavTimeTodayBtn };
        UpdateSidebarButtonGroup(buttons, clicked);
        ApplyFiltersAndRenderFeed(animateRows: true);
    }

    private static void UpdateSidebarButtonGroup(IEnumerable<Button> group, Button activeBtn)
    {
        foreach (var btn in group)
        {
            var isActive = ReferenceEquals(btn, activeBtn);
            btn.Background = isActive ? BrushSidebarActive : BrushTransparent;
            if (btn.Content is StackPanel sp)
            {
                foreach (var child in sp.Children)
                {
                    if (child is FontIcon fi)
                    {
                        fi.Foreground = isActive ? BrushTextPrimary : BrushTextSecondary;
                    }
                    else if (child is TextBlock tb)
                    {
                        tb.Foreground = isActive ? BrushTextPrimary : BrushTextSecondary;
                        tb.FontWeight = isActive
                            ? Microsoft.UI.Text.FontWeights.SemiBold
                            : Microsoft.UI.Text.FontWeights.Normal;
                    }
                }
            }
        }
    }

    private void OnSearchTextChanged(object sender, TextChangedEventArgs e)
    {
        _searchQuery = SearchBox.Text ?? "";
        ApplyFiltersAndRenderFeed(animateRows: false);
    }

    private async void OnInspectorCopyClick(object sender, RoutedEventArgs e)
    {
        try
        {
            await CopySelectedItemAsync();
        }
        catch (Exception ex)
        {
            ShowNotification("Không thể sao chép mục đã chọn: " + ex.Message, InfoBarSeverity.Error);
        }
    }

    private async void OnInspectorSendIphoneClick(object sender, RoutedEventArgs e)
    {
        try
        {
            await SendSelectedItemToIphoneAsync();
        }
        catch (Exception ex)
        {
            ShowNotification("Lỗi truyền nội dung sang iPhone: " + ex.Message, InfoBarSeverity.Error);
        }
    }

    private async void OnInspectorFavoriteClick(object sender, RoutedEventArgs e)
    {
        try
        {
            if (_selectedItem is null) return;
            await _backend.TogglePinAsync(_selectedItem.Id);
            _lastHistoryRevision = -1;
            await RefreshStatusAndHistoryAsync();
        }
        catch (Exception ex)
        {
            ShowNotification("Không thể ghim mục: " + ex.Message, InfoBarSeverity.Warning);
        }
    }

    private async void OnInspectorDeleteClick(object sender, RoutedEventArgs e)
    {
        try
        {
            if (_selectedItem is null) return;
            await _backend.DeleteItemAsync(_selectedItem.Id);
            _selectedItem = null;
            _lastHistoryRevision = -1;
            await RefreshStatusAndHistoryAsync();
        }
        catch (Exception ex)
        {
            ShowNotification("Không thể xóa mục: " + ex.Message, InfoBarSeverity.Error);
        }
    }

    private async void OnRootGridKeyDown(object sender, KeyRoutedEventArgs e)
    {
        try
        {
            if (FocusManager.GetFocusedElement(RootGrid.XamlRoot) is TextBox)
            {
                return;
            }

            if (e.Key == VirtualKey.C)
            {
                await CopySelectedItemAsync();
                e.Handled = true;
            }
            else if (e.Key == VirtualKey.S)
            {
                await SendSelectedItemToIphoneAsync();
                e.Handled = true;
            }
            else if (e.Key == VirtualKey.F && _selectedItem is not null)
            {
                await _backend.TogglePinAsync(_selectedItem.Id);
                _lastHistoryRevision = -1;
                await RefreshStatusAndHistoryAsync();
                e.Handled = true;
            }
            else if (e.Key == VirtualKey.D && _selectedItem is not null)
            {
                await _backend.DeleteItemAsync(_selectedItem.Id);
                _selectedItem = null;
                _lastHistoryRevision = -1;
                await RefreshStatusAndHistoryAsync();
                e.Handled = true;
            }
        }
        catch (Exception ex)
        {
            System.Diagnostics.Debug.WriteLine($"[OnRootGridKeyDown Error] {ex.Message}");
        }
    }

    private async void OnSyncNowClick(object sender, RoutedEventArgs e)
    {
        try
        {
            await _backend.TriggerSyncNowAsync();
            await Task.Delay(250);
            await RefreshStatusAndHistoryAsync();
            ShowNotification("Đã gửi tín hiệu đồng bộ tức thời tới iPhone.", InfoBarSeverity.Success);
        }
        catch (Exception ex)
        {
            ShowNotification("Không thể đồng bộ: " + ex.Message, InfoBarSeverity.Error);
        }
    }

    private async void OnTransportAutoClick(object sender, RoutedEventArgs e)
    {
        try
        {
            UpdateTransportButtons("auto");
            await _backend.SetTransportModeAsync("auto");
        }
        catch (Exception ex)
        {
            ShowNotification("Lỗi chuyển chế độ truyền: " + ex.Message, InfoBarSeverity.Warning);
        }
    }

    private async void OnTransportWifiClick(object sender, RoutedEventArgs e)
    {
        try
        {
            UpdateTransportButtons("wifi");
            await _backend.SetTransportModeAsync("wifi");
        }
        catch (Exception ex)
        {
            ShowNotification("Lỗi chuyển chế độ Wi-Fi: " + ex.Message, InfoBarSeverity.Warning);
        }
    }

    private async void OnTransportUsbClick(object sender, RoutedEventArgs e)
    {
        try
        {
            UpdateTransportButtons("usb");
            await _backend.SetTransportModeAsync("usb");
        }
        catch (Exception ex)
        {
            ShowNotification("Lỗi chuyển chế độ USB: " + ex.Message, InfoBarSeverity.Warning);
        }
    }

    private async void OnClearHistoryClick(object sender, RoutedEventArgs e)
    {
        try
        {
            await _backend.ClearHistoryAsync(includePinned: false);
            _selectedItem = null;
            _lastHistoryRevision = -1;
            await RefreshStatusAndHistoryAsync();
            ShowNotification("Đã dọn dẹp lịch sử clipboard.", InfoBarSeverity.Informational);
        }
        catch (Exception ex)
        {
            ShowNotification("Lỗi xóa lịch sử: " + ex.Message, InfoBarSeverity.Error);
        }
    }

    private async void OnPairWifiClick(object sender, RoutedEventArgs e)
    {
        try
        {
            PairWifiBtn.IsEnabled = false;
            PairWifiBtnText.Text = "Pairing over USB...";
            OnboardingFeedbackText.Text = "Establishing RemotePairing record over USB...";

            var (ok, message) = await _backend.PairWifiOverUsbAsync();
            PairWifiBtn.IsEnabled = true;
            PairWifiBtnText.Text = ok ? "Paired Wireless Sync" : "Pair Wireless Over USB";
            OnboardingFeedbackText.Text = message;
            ShowNotification(message, ok ? InfoBarSeverity.Success : InfoBarSeverity.Warning);

            await RefreshStatusAndHistoryAsync();
        }
        catch (Exception ex)
        {
            PairWifiBtn.IsEnabled = true;
            PairWifiBtnText.Text = "Pair Wireless Over USB";
            OnboardingFeedbackText.Text = "Pairing error: " + ex.Message;
            ShowNotification("Lỗi ghép đôi không dây: " + ex.Message, InfoBarSeverity.Error);
        }
    }

    private void OnWindowClosed(object sender, WindowEventArgs args)
    {
        if (_hwnd != IntPtr.Zero)
        {
            UnregisterHotKey(_hwnd, HotkeyIdWinAltV);
            UnregisterHotKey(_hwnd, HotkeyIdCtrlShiftV);
        }
        try
        {
            _airDropBle.StopAdvertising();
        }
        catch
        {
            // Ignore
        }
        try
        {
            _quickPasteWindow.Close();
        }
        catch
        {
            // Ignore if already closed
        }
        _pollTimer.Stop();
        _backend.Dispose();
    }

    [DllImport("user32.dll")]
    private static extern bool RegisterHotKey(IntPtr hWnd, int id, uint fsModifiers, uint vk);

    [DllImport("user32.dll")]
    private static extern bool UnregisterHotKey(IntPtr hWnd, int id);

    [DllImport("user32.dll", EntryPoint = "SetWindowLongPtrW")]
    private static extern IntPtr SetWindowLongPtrW(IntPtr hWnd, int nIndex, IntPtr dwNewLong);

    [DllImport("user32.dll", EntryPoint = "CallWindowProcW")]
    private static extern IntPtr CallWindowProcW(IntPtr lpPrevWndFunc, IntPtr hWnd, uint msg, IntPtr wParam, IntPtr lParam);

    [DllImport("user32.dll")]
    private static extern IntPtr GetForegroundWindow();

    [DllImport("user32.dll")]
    private static extern uint GetDpiForWindow(IntPtr hWnd);
}
