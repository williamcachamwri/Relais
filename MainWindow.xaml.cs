using System.Numerics;
using System.Runtime.InteropServices;
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
    private const int GwlpWndProc = -4;
    private const uint ModAlt = 0x0001;
    private const uint ModControl = 0x0002;
    private const uint ModShift = 0x0004;
    private const uint ModWin = 0x0008;
    private const uint ModNoRepeat = 0x4000;
    private const uint VkV = 0x56;

    private delegate IntPtr WndProcDelegate(IntPtr hWnd, uint msg, IntPtr wParam, IntPtr lParam);

    private readonly ClipboardBackendService _backend = new();
    private readonly DispatcherTimer _pollTimer;
    private readonly QuickPasteWindow _quickPasteWindow;
    private IntPtr _hwnd;
    private IntPtr _origWndProc;
    private WndProcDelegate? _wndProcDelegate;

    private List<ClipboardHistoryItem> _allItems = [];
    private List<ClipboardHistoryItem> _filteredItems = [];
    private ClipboardHistoryItem? _selectedItem;
    private BackendStatus? _lastStatus;
    private int _lastHistoryRevision = -1;

    private string _typeOrTagFilter = "type:all";
    private string _sourceFilter = "src:all";
    private string _timeFilter = "time:all";
    private string _searchQuery = "";
    private bool? _sidebarManualOverride;

    // Carbon-Graphite Palette (Reference Image #2)
    private static readonly SolidColorBrush BrushTextPrimary = new(ColorHelper.FromArgb(0xFF, 0xF2, 0xF2, 0xF5));
    private static readonly SolidColorBrush BrushTextSecondary = new(ColorHelper.FromArgb(0xFF, 0x9A, 0x9A, 0xA2));
    private static readonly SolidColorBrush BrushTextMuted = new(ColorHelper.FromArgb(0xFF, 0x6E, 0x6E, 0x76));
    private static readonly SolidColorBrush BrushTextDim = new(ColorHelper.FromArgb(0xFF, 0x5E, 0x5E, 0x66));
    private static readonly SolidColorBrush BrushWhite = new(Colors.White);

    private static readonly SolidColorBrush BrushFileGold = new(ColorHelper.FromArgb(0xFF, 0xB8, 0xB8, 0xC2));
    private static readonly SolidColorBrush BrushFileBadgeBg = new(ColorHelper.FromArgb(0xFF, 0x25, 0x25, 0x2C));
    private static readonly SolidColorBrush BrushLinkBlueText = new(ColorHelper.FromArgb(0xFF, 0x58, 0xA6, 0xFF));
    private static readonly SolidColorBrush BrushLinkBlueBg = new(ColorHelper.FromArgb(0xFF, 0x17, 0x2A, 0x3A));
    private static readonly SolidColorBrush BrushCodePurpleText = new(ColorHelper.FromArgb(0xFF, 0xA3, 0x71, 0xF7));
    private static readonly SolidColorBrush BrushCodePurpleBg = new(ColorHelper.FromArgb(0xFF, 0x23, 0x1C, 0x35));
    private static readonly SolidColorBrush BrushStatusGreen = new(ColorHelper.FromArgb(0xFF, 0x28, 0xC8, 0x40));
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

        RootGrid.Loaded += (_, _) => ApplyResponsiveLayout(RootGrid.ActualWidth > 0 ? RootGrid.ActualWidth : 1050, RootGrid.ActualHeight > 0 ? RootGrid.ActualHeight : 680);

        _pollTimer = new DispatcherTimer
        {
            Interval = TimeSpan.FromMilliseconds(650),
        };
        _pollTimer.Tick += OnPollTimerTick;

        Closed += OnWindowClosed;
        _ = InitializeBackendAsync();
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
            RailClipboardBtn, RailAboutBtn, RailSettingsBtn,
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

        // Reset manual override once window is resized wide enough
        if (width >= 960 && _sidebarManualOverride == true)
        {
            _sidebarManualOverride = null;
        }

        var showSidebar = _sidebarManualOverride ?? (width >= 920);
        if (width < 640)
        {
            showSidebar = false;
        }

        FilterSidebarBorder.Visibility = showSidebar ? Visibility.Visible : Visibility.Collapsed;
        ColSidebar.Width = showSidebar ? new GridLength(176) : new GridLength(0);

        // Dynamic Feed column width based on remaining horizontal space
        var remainingAfterSidebar = width - 50 - (showSidebar ? 176 : 0);
        var targetFeedWidth = Math.Clamp(remainingAfterSidebar * 0.40, 210, 330);
        ColFeed.Width = new GridLength(targetFeedWidth);

        // Top Bar responsive search box and subtitle visibility
        var showTopHint = width >= 820;
        TitleBarHintDot.Visibility = showTopHint ? Visibility.Visible : Visibility.Collapsed;
        TitleBarSyncHintText.Visibility = showTopHint ? Visibility.Visible : Visibility.Collapsed;
        SearchPillBorder.Width = Math.Clamp(width * 0.29, 165, 320);

        // Inspector responsive spacing, image bounds, centered text card width, and action bar labels
        var inspectorWidth = Math.Max(240, remainingAfterSidebar - targetFeedWidth);
        InspectorTopBar.Padding = inspectorWidth < 380 ? new Thickness(14, 0, 14, 0) : new Thickness(24, 0, 24, 0);
        InspectorStageGrid.Padding = inspectorWidth < 380 ? new Thickness(16, 12, 16, 12) : new Thickness(28, 16, 28, 16);

        InspectorTextStage.Width = Math.Clamp(inspectorWidth - 64, 220, 500);
        InspectorPreviewImage.MaxWidth = Math.Max(180, inspectorWidth - 64);
        InspectorPreviewImage.MaxHeight = Math.Max(160, height - 210);

        var compactBar = inspectorWidth < 430;
        var veryCompactBar = inspectorWidth < 340;
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
                AnimateScale(fe, pressedScale, 95);
            }
        };
        element.PointerReleased += (s, _) =>
        {
            if (s is FrameworkElement fe)
            {
                AnimateScale(fe, 1.0f, 150);
            }
        };
        element.PointerExited += (s, _) =>
        {
            if (s is FrameworkElement fe)
            {
                AnimateScale(fe, 1.0f, 150);
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
            var easing = compositor.CreateCubicBezierEasingFunction(new Vector2(0.22f, 1.0f), new Vector2(0.36f, 1.0f));
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
            var easing = compositor.CreateCubicBezierEasingFunction(new Vector2(0.22f, 1.0f), new Vector2(0.36f, 1.0f));

            var fade = compositor.CreateScalarKeyFrameAnimation();
            fade.InsertKeyFrame(0.0f, 0.0f);
            fade.InsertKeyFrame(1.0f, 1.0f, easing);
            fade.Duration = TimeSpan.FromMilliseconds(175);
            if (delayMs > 0) fade.DelayTime = TimeSpan.FromMilliseconds(delayMs);

            var slide = compositor.CreateVector3KeyFrameAnimation();
            slide.InsertKeyFrame(0.0f, new Vector3(0f, 6f, 0f));
            slide.InsertKeyFrame(1.0f, Vector3.Zero, easing);
            slide.Duration = TimeSpan.FromMilliseconds(175);
            if (delayMs > 0) slide.DelayTime = TimeSpan.FromMilliseconds(delayMs);

            visual.StartAnimation("Opacity", fade);
            visual.StartAnimation("Offset", slide);
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
        await RefreshStatusAndHistoryAsync();
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
                AnimateStageEntrance(row, delayMs: i * 16);
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

        if (item.IsImage || item.IsFile)
        {
            leftMeta.Children.Add(new FontIcon
            {
                Glyph = "\uE7C3",
                FontSize = 11.5,
                Foreground = BrushFileGold,
            });
            leftMeta.Children.Add(new TextBlock
            {
                Text = "File",
                FontSize = 12,
                FontWeight = Microsoft.UI.Text.FontWeights.Medium,
                Foreground = BrushFileGold,
            });
        }
        else
        {
            leftMeta.Children.Add(new FontIcon
            {
                Glyph = "\uE8A5",
                FontSize = 11.5,
                Foreground = BrushTextSecondary,
            });
            leftMeta.Children.Add(new TextBlock
            {
                Text = "Text",
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
                Foreground = BrushFileGold,
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
            InspectorPreviewText.Text = "Copy text, images, URLs, or files on your iPhone or Windows PC — both directions sync automatically.";
            InspectorTextSubtitle.Text = "";
            return;
        }

        InspectorSourceIcon.Glyph = item.IsFromPc ? "\uE7F8" : "\uE8EA";
        InspectorSourceText.Text = item.IsFromPc ? "This device" : item.ShortDeviceName;
        ActionFavoriteIcon.Glyph = item.Pinned ? "\uE735" : "\uE734";
        ActionFavoriteIcon.Foreground = item.Pinned ? BrushFileGold : BrushTextSecondary;
        ActionFavoriteLabel.Text = item.Pinned ? "Favorited" : "Favorite";

        var direction = item.IsFromPc ? "Windows → iPhone" : "iPhone → Windows";

        if (item.IsImage)
        {
            InspectorMetaLeftText.Text = "File  ·  1 files";
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

            InspectorImageTitleText.Text = item.ImageDisplayName;
            if (animateStage) AnimateStageEntrance(InspectorImageStage);
        }
        else if (item.IsFile)
        {
            InspectorMetaLeftText.Text = "File  ·  1 files";
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
            InspectorMetaLeftText.Text = $"Text  ·  {item.Text.Length} chars";
            InspectorImageStage.Visibility = Visibility.Collapsed;
            InspectorFileStage.Visibility = Visibility.Collapsed;
            InspectorTextStage.Visibility = Visibility.Visible;

            InspectorPreviewText.Text = item.Text;
            InspectorPreviewText.FontFamily = item.IsCode
                ? new FontFamily("Cascadia Mono, Consolas")
                : new FontFamily("Segoe UI Variable Text, Segoe UI");
            InspectorPreviewText.FontSize = item.IsCode ? 13 : 14;
            InspectorTextSubtitle.Text = $"{direction}  ·  {item.Uti}  ·  {item.FormattedSize}  ·  {item.Transport}  ·  {item.LatencyMs:F1} ms";
            if (animateStage) AnimateStageEntrance(InspectorTextStage);
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
            InspectorPane.Visibility = Visibility.Visible;
            RailClipboardBtn.Background = BrushRailActive;
            RailAboutBtn.Background = BrushTransparent;
            RailSettingsBtn.Background = BrushTransparent;
            AnimateStageEntrance(InspectorPane);
        }
    }

    private void ShowOnboardingPane()
    {
        InspectorPane.Visibility = Visibility.Collapsed;
        AboutPane.Visibility = Visibility.Collapsed;
        OnboardingPane.Visibility = Visibility.Visible;
        RailSettingsBtn.Background = BrushRailActive;
        RailAboutBtn.Background = BrushTransparent;
        RailClipboardBtn.Background = BrushTransparent;
        AnimateStageEntrance(OnboardingPane);
    }

    private void ShowAboutPane()
    {
        InspectorPane.Visibility = Visibility.Collapsed;
        OnboardingPane.Visibility = Visibility.Collapsed;
        AboutPane.Visibility = Visibility.Visible;
        RailAboutBtn.Background = BrushRailActive;
        RailSettingsBtn.Background = BrushTransparent;
        RailClipboardBtn.Background = BrushTransparent;
        AnimateStageEntrance(AboutPane);
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
        await CopySelectedItemAsync();
    }

    private async void OnInspectorSendIphoneClick(object sender, RoutedEventArgs e)
    {
        await SendSelectedItemToIphoneAsync();
    }

    private async void OnInspectorFavoriteClick(object sender, RoutedEventArgs e)
    {
        if (_selectedItem is null) return;
        await _backend.TogglePinAsync(_selectedItem.Id);
        _lastHistoryRevision = -1;
        await RefreshStatusAndHistoryAsync();
    }

    private async void OnInspectorDeleteClick(object sender, RoutedEventArgs e)
    {
        if (_selectedItem is null) return;
        await _backend.DeleteItemAsync(_selectedItem.Id);
        _selectedItem = null;
        _lastHistoryRevision = -1;
        await RefreshStatusAndHistoryAsync();
    }

    private async void OnRootGridKeyDown(object sender, KeyRoutedEventArgs e)
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

    private async void OnSyncNowClick(object sender, RoutedEventArgs e)
    {
        await _backend.TriggerSyncNowAsync();
        await Task.Delay(250);
        await RefreshStatusAndHistoryAsync();
    }

    private async void OnTransportAutoClick(object sender, RoutedEventArgs e)
    {
        UpdateTransportButtons("auto");
        await _backend.SetTransportModeAsync("auto");
    }

    private async void OnTransportWifiClick(object sender, RoutedEventArgs e)
    {
        UpdateTransportButtons("wifi");
        await _backend.SetTransportModeAsync("wifi");
    }

    private async void OnTransportUsbClick(object sender, RoutedEventArgs e)
    {
        UpdateTransportButtons("usb");
        await _backend.SetTransportModeAsync("usb");
    }

    private async void OnClearHistoryClick(object sender, RoutedEventArgs e)
    {
        await _backend.ClearHistoryAsync(includePinned: false);
        _selectedItem = null;
        _lastHistoryRevision = -1;
        await RefreshStatusAndHistoryAsync();
    }

    private async void OnPairWifiClick(object sender, RoutedEventArgs e)
    {
        PairWifiBtn.IsEnabled = false;
        PairWifiBtnText.Text = "Pairing over USB...";
        OnboardingFeedbackText.Text = "Establishing RemotePairing record over USB...";

        var (ok, message) = await _backend.PairWifiOverUsbAsync();
        PairWifiBtn.IsEnabled = true;
        PairWifiBtnText.Text = ok ? "Paired Wireless Sync" : "Pair Wireless Over USB";
        OnboardingFeedbackText.Text = message;

        await RefreshStatusAndHistoryAsync();
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
}
