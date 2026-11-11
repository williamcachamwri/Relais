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

namespace UniversalClipboard;

public sealed partial class QuickPasteWindow : Window
{
    private const double PopupWidthDip = 408;
    private const double PopupHeightDip = 440;
    private const int ScreenMarginPx = 14;

    private const int GwlpWndProc = -4;
    private const int GwlStyle = -16;
    private const uint WmNcCalcSize = 0x0083;
    private const uint WmNcActivate = 0x0086;
    private const long WsCaption = 0x00C00000L;
    private const long WsThickFrame = 0x00040000L;
    private const long WsMinimizeBox = 0x00020000L;
    private const long WsMaximizeBox = 0x00010000L;
    private const long WsSysMenu = 0x00080000L;
    private const uint SwpNoSize = 0x0001;
    private const uint SwpNoMove = 0x0002;
    private const uint SwpNoZOrder = 0x0004;
    private const uint SwpNoActivate = 0x0010;
    private const uint SwpFrameChanged = 0x0020;

    // DWM attributes
    private const int DwmwaUseImmersiveDarkMode = 20;
    private const int DwmwaWindowCornerPreference = 33;
    private const int DwmwaBorderColor = 34;
    private const int DwmwcpRound = 2;

    private delegate IntPtr WndProcDelegate(IntPtr hWnd, uint msg, IntPtr wParam, IntPtr lParam);

    private readonly ClipboardBackendService _backend;
    private List<ClipboardHistoryItem> _allItems = [];
    private List<ClipboardHistoryItem> _filteredItems = [];
    private int _selectedIndex;
    private string _activeTab = "all";
    private string _searchQuery = "";
    private IntPtr _targetForegroundHwnd = IntPtr.Zero;
    private AppWindow? _appWindow;
    private IntPtr _hwnd;
    private IntPtr _origWndProc;
    private WndProcDelegate? _wndProcDelegate;
    private bool _isClosingOrPasting;

    private static readonly SolidColorBrush BrushTextPrimary = Hex(0xF2, 0xF2, 0xF5);
    private static readonly SolidColorBrush BrushTextRow = Hex(0xD2, 0xD2, 0xDA);
    private static readonly SolidColorBrush BrushTextMuted = Hex(0x66, 0x66, 0x70);
    private static readonly SolidColorBrush BrushIcon = Hex(0x8C, 0x8C, 0x95);
    private static readonly SolidColorBrush BrushNeutralAccent = Hex(0xC8, 0xC8, 0xD2);
    private static readonly SolidColorBrush BrushLinkBlue = Hex(0x6C, 0xA8, 0xF0);

    private static readonly SolidColorBrush BrushTabActiveBg = Hex(0xED, 0xED, 0xF0);
    private static readonly SolidColorBrush BrushTabActiveFg = Hex(0x12, 0x12, 0x14);
    private static readonly SolidColorBrush BrushTabInactiveBg = Hex(0x1E, 0x1E, 0x24);
    private static readonly SolidColorBrush BrushTabInactiveFg = Hex(0x8E, 0x8E, 0x98);

    private static readonly SolidColorBrush BrushRowSelectedBg = Hex(0x25, 0x25, 0x2D);
    private static readonly SolidColorBrush BrushRowSelectedBorder = Hex(0x36, 0x36, 0x42);
    private static readonly SolidColorBrush BrushRowHoverBg = Hex(0x1C, 0x1C, 0x22);
    private static readonly SolidColorBrush BrushTransparent = new(Colors.Transparent);

    private static readonly SolidColorBrush BrushTileBg = Hex(0x1C, 0x1C, 0x22);
    private static readonly SolidColorBrush BrushTileBorder = Hex(0x2C, 0x2C, 0x34);

    private static readonly SolidColorBrush BrushKeyBg = Hex(0x1B, 0x1B, 0x21);
    private static readonly SolidColorBrush BrushKeyBorder = Hex(0x2C, 0x2C, 0x34);
    private static readonly SolidColorBrush BrushKeyFg = Hex(0x78, 0x78, 0x82);
    private static readonly SolidColorBrush BrushKeySelBg = Hex(0x32, 0x32, 0x3C);
    private static readonly SolidColorBrush BrushKeySelBorder = Hex(0x48, 0x48, 0x56);
    private static readonly SolidColorBrush BrushKeySelFg = Hex(0xEC, 0xEC, 0xF2);

    private readonly List<Border> _rowBorders = [];

    public QuickPasteWindow(ClipboardBackendService backend)
    {
        _backend = backend;
        InitializeComponent();
        ConfigurePopupChrome();
        UpdateTabVisuals();
        Activated += OnWindowActivated;
    }

    public bool IsPopupVisible => _appWindow?.IsVisible ?? false;

    private static SolidColorBrush Hex(byte r, byte g, byte b) => new(ColorHelper.FromArgb(0xFF, r, g, b));

    private void ConfigurePopupChrome()
    {
        ExtendsContentIntoTitleBar = true;
        _hwnd = WinRT.Interop.WindowNative.GetWindowHandle(this);
        var windowId = Win32Interop.GetWindowIdFromWindow(_hwnd);
        _appWindow = AppWindow.GetFromWindowId(windowId);

        var iconPath = Path.Combine(AppContext.BaseDirectory, "Assets", "app.ico");
        if (File.Exists(iconPath))
        {
            _appWindow.SetIcon(iconPath);
        }

        if (_appWindow.Presenter is OverlappedPresenter presenter)
        {
            presenter.IsAlwaysOnTop = true;
            presenter.IsMaximizable = false;
            presenter.IsMinimizable = false;
            presenter.IsResizable = false;
            presenter.SetBorderAndTitleBar(false, false);
        }

        if (AppWindowTitleBar.IsCustomizationSupported() && _appWindow.TitleBar is { } titleBar)
        {
            titleBar.ExtendsContentIntoTitleBar = true;
            titleBar.ButtonBackgroundColor = Colors.Transparent;
            titleBar.ButtonInactiveBackgroundColor = Colors.Transparent;
        }

        // Subclass WndProc to return 0 on WM_NCCALCSIZE (eliminates the white Win32 caption bar completely)
        _wndProcDelegate = PopupWndProc;
        var fnPtr = Marshal.GetFunctionPointerForDelegate(_wndProcDelegate);
        _origWndProc = SetWindowLongPtrW(_hwnd, GwlpWndProc, fnPtr);

        // Strip caption/sysmenu/thickframe styles so Win32 never paints a title bar
        var style = GetWindowLongPtrW(_hwnd, GwlStyle).ToInt64();
        style &= ~(WsCaption | WsThickFrame | WsMinimizeBox | WsMaximizeBox | WsSysMenu);
        SetWindowLongPtrW(_hwnd, GwlStyle, new IntPtr(style));

        SetWindowPos(_hwnd, IntPtr.Zero, 0, 0, 0, 0, SwpNoMove | SwpNoSize | SwpNoZOrder | SwpNoActivate | SwpFrameChanged);

        ApplyDwmRoundingAndBorder();
    }

    private void ApplyDwmRoundingAndBorder()
    {
        if (_hwnd == IntPtr.Zero) return;

        var dark = 1;
        DwmSetWindowAttribute(_hwnd, DwmwaUseImmersiveDarkMode, ref dark, sizeof(int));

        var corner = DwmwcpRound;
        DwmSetWindowAttribute(_hwnd, DwmwaWindowCornerPreference, ref corner, sizeof(int));

        // Subtle dark border color as COLORREF (0x00BBGGRR) -> #2B2B33
        var borderColor = 0x00332B2B;
        DwmSetWindowAttribute(_hwnd, DwmwaBorderColor, ref borderColor, sizeof(int));
    }

    private IntPtr PopupWndProc(IntPtr hWnd, uint msg, IntPtr wParam, IntPtr lParam)
    {
        if (msg == WmNcCalcSize && wParam != IntPtr.Zero)
        {
            // Returning 0 tells Windows the entire window is client area (no white caption bar)
            return IntPtr.Zero;
        }
        if (msg == WmNcActivate)
        {
            // Prevent Win32 from painting a classic non-client border on activation change
            return new IntPtr(1);
        }
        return CallWindowProcW(_origWndProc, hWnd, msg, wParam, lParam);
    }

    public void ShowPopup(IntPtr previousForegroundHwnd, List<ClipboardHistoryItem> items, BackendStatus? status)
    {
        if (previousForegroundHwnd != IntPtr.Zero && previousForegroundHwnd != _hwnd)
        {
            _targetForegroundHwnd = previousForegroundHwnd;
        }

        _allItems = items;
        _searchQuery = "";
        QuickSearchBox.Text = "";
        _selectedIndex = 0;

        PositionAwayFromCaret(previousForegroundHwnd);
        ApplyDwmRoundingAndBorder();
        ApplyFiltersAndRender();

        _appWindow?.Show();
        SetForegroundWindow(_hwnd);

        AnimatePopupEntrance();
        QuickSearchBox.Focus(FocusState.Programmatic);
        QuickListScrollViewer.ChangeView(null, 0, null, true);
    }

    public void HidePopup()
    {
        _appWindow?.Hide();
    }

    public void UpdateItemsIfVisible(List<ClipboardHistoryItem> items)
    {
        _allItems = items;
        if (IsPopupVisible)
        {
            ApplyFiltersAndRender();
        }
    }

    // -----------------------------------------------------------------------
    // Positioning: anchor beside/below caret or cursor so it never covers text
    // -----------------------------------------------------------------------

    private void PositionAwayFromCaret(IntPtr targetHwnd)
    {
        if (_appWindow is null) return;

        var hasCaret = TryGetCaretScreenRect(targetHwnd, out var anchor);
        if (!hasCaret)
        {
            anchor = GetCursorAnchorRect();
        }

        var displayArea = DisplayArea.GetFromPoint(new PointInt32(anchor.Left, anchor.Bottom), DisplayAreaFallback.Primary);
        var work = displayArea.WorkArea;

        var scale = GetScaleForPoint(anchor.Left, anchor.Bottom);
        var w = (int)Math.Round(PopupWidthDip * scale);
        var h = (int)Math.Round(PopupHeightDip * scale);
        var gap = (int)Math.Round(16 * scale);

        // Place slightly to the right and below the caret/cursor so the active line remains visible
        var x = anchor.Left + gap;
        var y = anchor.Bottom + gap;

        if (x + w > work.X + work.Width - ScreenMarginPx)
        {
            x = Math.Max(work.X + ScreenMarginPx, anchor.Left - w - gap);
        }

        if (y + h > work.Y + work.Height - ScreenMarginPx)
        {
            y = Math.Max(work.Y + ScreenMarginPx, anchor.Top - h - gap);
        }

        x = Math.Clamp(x, work.X + ScreenMarginPx, work.X + work.Width - w - ScreenMarginPx);
        y = Math.Clamp(y, work.Y + ScreenMarginPx, work.Y + work.Height - h - ScreenMarginPx);

        _appWindow.MoveAndResize(new RectInt32(x, y, w, h));
    }

    private static RECT GetCursorAnchorRect()
    {
        if (GetCursorPos(out var pt))
        {
            return new RECT { Left = pt.X, Top = pt.Y - 4, Right = pt.X + 4, Bottom = pt.Y + 20 };
        }
        var work = DisplayArea.Primary.WorkArea;
        var cx = work.X + work.Width - 440;
        var cy = work.Y + 80;
        return new RECT { Left = cx, Top = cy, Right = cx + 1, Bottom = cy + 1 };
    }

    private static bool TryGetCaretScreenRect(IntPtr targetHwnd, out RECT rect)
    {
        rect = default;
        if (targetHwnd == IntPtr.Zero) return false;

        var threadId = GetWindowThreadProcessId(targetHwnd, out _);
        var info = new GUITHREADINFO { cbSize = Marshal.SizeOf<GUITHREADINFO>() };
        if (!GetGUIThreadInfo(threadId, ref info) || info.hwndCaret == IntPtr.Zero)
        {
            return false;
        }

        var caret = info.rcCaret;
        if (caret.Right == 0 && caret.Bottom == 0) return false;

        var topLeft = new POINT { X = caret.Left, Y = caret.Top };
        var bottomRight = new POINT { X = caret.Right, Y = caret.Bottom };
        ClientToScreen(info.hwndCaret, ref topLeft);
        ClientToScreen(info.hwndCaret, ref bottomRight);

        rect = new RECT { Left = topLeft.X, Top = topLeft.Y, Right = bottomRight.X, Bottom = bottomRight.Y };
        return true;
    }

    private static double GetScaleForPoint(int x, int y)
    {
        try
        {
            var monitor = MonitorFromPoint(new POINT { X = x, Y = y }, 2);
            if (GetDpiForMonitor(monitor, 0, out var dpiX, out _) == 0 && dpiX > 0)
            {
                return dpiX / 96.0;
            }
        }
        catch
        {
            // Default to 1.0
        }
        return 1.0;
    }

    private void AnimatePopupEntrance()
    {
        try
        {
            var visual = ElementCompositionPreview.GetElementVisual(PopupRoot);
            var compositor = visual.Compositor;
            var easing = compositor.CreateCubicBezierEasingFunction(new Vector2(0.16f, 1.0f), new Vector2(0.3f, 1.0f));

            var fade = compositor.CreateScalarKeyFrameAnimation();
            fade.InsertKeyFrame(0.0f, 0.0f);
            fade.InsertKeyFrame(1.0f, 1.0f, easing);
            fade.Duration = TimeSpan.FromMilliseconds(125);

            var slide = compositor.CreateVector3KeyFrameAnimation();
            slide.InsertKeyFrame(0.0f, new Vector3(0f, 5f, 0f));
            slide.InsertKeyFrame(1.0f, Vector3.Zero, easing);
            slide.Duration = TimeSpan.FromMilliseconds(140);

            visual.StartAnimation("Opacity", fade);
            visual.StartAnimation("Offset", slide);
        }
        catch
        {
            // Ignore composition error
        }
    }

    private void OnWindowActivated(object sender, WindowActivatedEventArgs args)
    {
        if (args.WindowActivationState == WindowActivationState.Deactivated && !_isClosingOrPasting)
        {
            HidePopup();
        }
    }

    // -----------------------------------------------------------------------
    // Rendering
    // -----------------------------------------------------------------------

    private void ApplyFiltersAndRender()
    {
        _filteredItems = _allItems.Where(item =>
        {
            var matchesTab = _activeTab switch
            {
                "text" => !item.IsImage && !item.IsFile,
                "richtext" => item.IsRichText,
                "image" => item.IsImage,
                "file" => item.IsFile || item.IsImage,
                _ => true,
            };
            if (!matchesTab) return false;

            if (string.IsNullOrWhiteSpace(_searchQuery)) return true;
            var q = _searchQuery.Trim();
            return item.Text.Contains(q, StringComparison.OrdinalIgnoreCase)
                   || item.FileDisplayName.Contains(q, StringComparison.OrdinalIgnoreCase)
                   || item.Uti.Contains(q, StringComparison.OrdinalIgnoreCase);
        }).ToList();

        QuickItemCountText.Text = _filteredItems.Count == 1 ? "1 item" : $"{_filteredItems.Count} items";
        QuickEmptyState.Visibility = _filteredItems.Count == 0 ? Visibility.Visible : Visibility.Collapsed;

        if (_selectedIndex >= _filteredItems.Count)
        {
            _selectedIndex = Math.Max(0, _filteredItems.Count - 1);
        }

        QuickItemsPanel.Children.Clear();
        _rowBorders.Clear();

        for (var i = 0; i < _filteredItems.Count; i++)
        {
            var row = BuildQuickRow(_filteredItems[i], i);
            _rowBorders.Add(row);
            QuickItemsPanel.Children.Add(row);
        }

        UpdateFooterForSelection();
    }

    private Border BuildQuickRow(ClipboardHistoryItem item, int index)
    {
        var isSelected = index == _selectedIndex;
        var rowBorder = new Border
        {
            MinHeight = 40,
            CornerRadius = new CornerRadius(8),
            Padding = new Thickness(9, 6, 10, 6),
            Background = isSelected ? BrushRowSelectedBg : BrushTransparent,
            BorderBrush = isSelected ? BrushRowSelectedBorder : BrushTransparent,
            BorderThickness = new Thickness(1),
        };

        rowBorder.PointerEntered += (_, _) =>
        {
            if (_selectedIndex != index) rowBorder.Background = BrushRowHoverBg;
        };
        rowBorder.PointerExited += (_, _) =>
        {
            if (_selectedIndex != index) rowBorder.Background = BrushTransparent;
        };
        rowBorder.Tapped += async (_, _) =>
        {
            SetSelection(index, scrollIntoView: false);
            await PasteItemAtIndexAsync(index);
        };

        var grid = new Grid { ColumnSpacing = 10 };
        grid.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
        grid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
        grid.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });

        // Leading tile: thumbnail for images, glyph tile otherwise
        var tile = new Border
        {
            Width = 28,
            Height = 28,
            CornerRadius = new CornerRadius(6),
            Background = BrushTileBg,
            BorderBrush = BrushTileBorder,
            BorderThickness = new Thickness(1),
            VerticalAlignment = VerticalAlignment.Center,
        };

        if (item.IsImage)
        {
            try
            {
                tile.Child = new Image
                {
                    Source = new BitmapImage(new Uri(_backend.GetImageUrl(item.Id))) { DecodePixelWidth = 56 },
                    Stretch = Stretch.UniformToFill,
                };
            }
            catch
            {
                tile.Child = GlyphFor("\uEB9F", BrushIcon);
            }
        }
        else if (item.IsFile)
        {
            tile.Child = new TextBlock
            {
                Text = ShortExt(item.FileExtensionBadge),
                FontSize = 8.5,
                FontWeight = Microsoft.UI.Text.FontWeights.Bold,
                Foreground = BrushNeutralAccent,
                HorizontalAlignment = HorizontalAlignment.Center,
                VerticalAlignment = VerticalAlignment.Center,
            };
        }
        else if (item.IsLink)
        {
            tile.Child = GlyphFor("\uE71B", BrushLinkBlue);
        }
        else
        {
            tile.Child = GlyphFor("\uE8C1", BrushIcon);
        }

        Grid.SetColumn(tile, 0);
        grid.Children.Add(tile);

        // Title + secondary meta line
        var textStack = new StackPanel { Spacing = 1, VerticalAlignment = VerticalAlignment.Center };
        textStack.Children.Add(new TextBlock
        {
            Text = TitleFor(item),
            FontSize = 13,
            FontWeight = isSelected ? Microsoft.UI.Text.FontWeights.SemiBold : Microsoft.UI.Text.FontWeights.Medium,
            Foreground = isSelected ? BrushTextPrimary : BrushTextRow,
            TextTrimming = TextTrimming.CharacterEllipsis,
            TextWrapping = TextWrapping.NoWrap,
            MaxLines = 1,
        });
        var meta = new TextBlock
        {
            Text = MetaFor(item),
            FontSize = 11,
            Foreground = BrushTextMuted,
            TextTrimming = TextTrimming.CharacterEllipsis,
            TextWrapping = TextWrapping.NoWrap,
            MaxLines = 1,
        };
        Microsoft.UI.Xaml.Documents.Typography.SetNumeralAlignment(meta, FontNumeralAlignment.Tabular);
        textStack.Children.Add(meta);

        Grid.SetColumn(textStack, 1);
        grid.Children.Add(textStack);

        // Shortcut pill badge (^1 .. ^9)
        if (index < 9)
        {
            var keyText = new TextBlock
            {
                Text = $"^{index + 1}",
                FontSize = 11,
                FontWeight = Microsoft.UI.Text.FontWeights.SemiBold,
                Foreground = isSelected ? BrushKeySelFg : BrushKeyFg,
                HorizontalAlignment = HorizontalAlignment.Center,
                VerticalAlignment = VerticalAlignment.Center,
            };
            Microsoft.UI.Xaml.Documents.Typography.SetNumeralAlignment(keyText, FontNumeralAlignment.Tabular);

            var keycap = new Border
            {
                MinWidth = 26,
                Height = 20,
                CornerRadius = new CornerRadius(10),
                Padding = new Thickness(6, 0, 6, 0),
                Background = isSelected ? BrushKeySelBg : BrushKeyBg,
                BorderBrush = isSelected ? BrushKeySelBorder : BrushKeyBorder,
                BorderThickness = new Thickness(1),
                VerticalAlignment = VerticalAlignment.Center,
                Child = keyText,
            };
            Grid.SetColumn(keycap, 2);
            grid.Children.Add(keycap);
        }

        rowBorder.Child = grid;
        return rowBorder;
    }

    private static FontIcon GlyphFor(string glyph, Brush brush) => new()
    {
        Glyph = glyph,
        FontSize = 12,
        Foreground = brush,
        HorizontalAlignment = HorizontalAlignment.Center,
        VerticalAlignment = VerticalAlignment.Center,
    };

    private static string ShortExt(string ext) => ext.Length > 4 ? ext[..4] : ext;

    private static string TitleFor(ClipboardHistoryItem item)
    {
        if (item.IsImage)
        {
            return !string.IsNullOrWhiteSpace(item.FileName) ? item.FileName : "Image";
        }
        if (item.IsFile)
        {
            return item.FileDisplayName;
        }
        return CollapseSingleLine(item.Text);
    }

    private static string MetaFor(ClipboardHistoryItem item)
    {
        var source = item.IsFromPc ? "This device" : item.ShortDeviceName;
        var time = FormatCompactTime(item.Timestamp);
        string kind;
        if (item.IsImage)
        {
            kind = item.Width > 0 && item.Height > 0 ? $"{item.Width}×{item.Height}" : "Image";
        }
        else if (item.IsFile)
        {
            kind = item.FormattedSize;
        }
        else if (item.IsLink)
        {
            kind = "Link";
        }
        else
        {
            kind = $"{item.Text.Length} chars";
        }
        return $"{kind}  ·  {source}  ·  {time}";
    }

    private void SetSelection(int newIndex, bool scrollIntoView)
    {
        if (_filteredItems.Count == 0) return;
        newIndex = Math.Clamp(newIndex, 0, _filteredItems.Count - 1);
        if (newIndex == _selectedIndex) return;

        var old = _selectedIndex;
        _selectedIndex = newIndex;

        RebuildRow(old);
        RebuildRow(newIndex);
        UpdateFooterForSelection();

        if (scrollIntoView && newIndex < _rowBorders.Count)
        {
            _rowBorders[newIndex].StartBringIntoView(new BringIntoViewOptions
            {
                AnimationDesired = true,
                VerticalAlignmentRatio = 0.5,
            });
        }
    }

    private void RebuildRow(int index)
    {
        if (index < 0 || index >= _filteredItems.Count || index >= _rowBorders.Count) return;
        var row = BuildQuickRow(_filteredItems[index], index);
        _rowBorders[index] = row;
        QuickItemsPanel.Children[index] = row;
    }

    private void UpdateFooterForSelection()
    {
        if (_selectedIndex >= 0 && _selectedIndex < _filteredItems.Count)
        {
            var item = _filteredItems[_selectedIndex];
            FooterSourceIcon.Glyph = item.IsFromPc ? "\uE7F8" : "\uE8EA";
            var src = item.IsFromPc ? "This device" : item.ShortDeviceName;
            FooterSourceText.Text = $"{src}  ·  {item.FormattedSize}";
        }
        else
        {
            FooterSourceIcon.Glyph = "\uE81E";
            FooterSourceText.Text = "Relais";
        }
    }

    private async Task PasteItemAtIndexAsync(int index)
    {
        if (index < 0 || index >= _filteredItems.Count || _isClosingOrPasting) return;
        _isClosingOrPasting = true;

        try
        {
            var item = _filteredItems[index];
            var copied = await _backend.CopyItemToWindowsAsync(item.Id);
            HidePopup();

            if (copied && _targetForegroundHwnd != IntPtr.Zero && _targetForegroundHwnd != _hwnd)
            {
                SetForegroundWindow(_targetForegroundHwnd);
                await Task.Delay(70);
                SynthesizeCtrlV();
            }
        }
        finally
        {
            _isClosingOrPasting = false;
        }
    }

    private static void SynthesizeCtrlV()
    {
        const byte VK_CONTROL = 0x11;
        const byte VK_MENU = 0x12;
        const byte VK_LWIN = 0x5B;
        const byte VK_V = 0x56;
        const uint KEYEVENTF_KEYUP = 0x0002;

        keybd_event(VK_MENU, 0, KEYEVENTF_KEYUP, UIntPtr.Zero);
        keybd_event(VK_LWIN, 0, KEYEVENTF_KEYUP, UIntPtr.Zero);

        keybd_event(VK_CONTROL, 0, 0, UIntPtr.Zero);
        keybd_event(VK_V, 0, 0, UIntPtr.Zero);
        keybd_event(VK_V, 0, KEYEVENTF_KEYUP, UIntPtr.Zero);
        keybd_event(VK_CONTROL, 0, KEYEVENTF_KEYUP, UIntPtr.Zero);
    }

    private void OnTabFilterClick(object sender, RoutedEventArgs e)
    {
        if (sender is not Button clicked || clicked.Tag is not string tab) return;
        SetActiveTab(tab);
    }

    private void SetActiveTab(string tab)
    {
        _activeTab = tab;
        _selectedIndex = 0;
        UpdateTabVisuals();
        ApplyFiltersAndRender();
        QuickListScrollViewer.ChangeView(null, 0, null, true);
        QuickSearchBox.Focus(FocusState.Programmatic);
    }

    private void UpdateTabVisuals()
    {
        var tabs = new[] { TabAllBtn, TabTextBtn, TabRichTextBtn, TabImageBtn, TabFileBtn };
        foreach (var btn in tabs)
        {
            var isActive = string.Equals(btn.Tag as string, _activeTab, StringComparison.OrdinalIgnoreCase);
            btn.Background = isActive ? BrushTabActiveBg : BrushTabInactiveBg;
            if (btn.Content is TextBlock tb)
            {
                tb.Foreground = isActive ? BrushTabActiveFg : BrushTabInactiveFg;
                tb.FontWeight = isActive ? Microsoft.UI.Text.FontWeights.SemiBold : Microsoft.UI.Text.FontWeights.Medium;
                tb.FontSize = 12;
            }
        }
    }

    private void OnQuickSearchTextChanged(object sender, TextChangedEventArgs e)
    {
        _searchQuery = QuickSearchBox.Text ?? "";
        _selectedIndex = 0;
        ApplyFiltersAndRender();
    }

    private async void OnSearchBoxPreviewKeyDown(object sender, KeyRoutedEventArgs e)
    {
        try
        {
            if (await HandleNavigationKeyAsync(e.Key)) e.Handled = true;
        }
        catch (Exception ex)
        {
            System.Diagnostics.Debug.WriteLine($"[QuickPaste Navigation Error] {ex.Message}");
        }
    }

    private async void OnRootKeyDown(object sender, KeyRoutedEventArgs e)
    {
        try
        {
            if (await HandleNavigationKeyAsync(e.Key)) e.Handled = true;
        }
        catch (Exception ex)
        {
            System.Diagnostics.Debug.WriteLine($"[QuickPaste RootKey Error] {ex.Message}");
        }
    }

    private async Task<bool> HandleNavigationKeyAsync(VirtualKey key)
    {
        switch (key)
        {
            case VirtualKey.Escape:
                HidePopup();
                return true;
            case VirtualKey.Down:
                SetSelection(_selectedIndex + 1, scrollIntoView: true);
                return true;
            case VirtualKey.Up:
                SetSelection(_selectedIndex - 1, scrollIntoView: true);
                return true;
            case VirtualKey.Enter:
                await PasteItemAtIndexAsync(_selectedIndex);
                return true;
            case VirtualKey.Tab:
            {
                var order = new[] { "all", "text", "richtext", "image" };
                var idx = Array.IndexOf(order, _activeTab);
                SetActiveTab(order[(idx + 1) % order.Length]);
                return true;
            }
        }

        var ctrlDown = (GetAsyncKeyState(0x11) & 0x8000) != 0;
        var altDown = (GetAsyncKeyState(0x12) & 0x8000) != 0;
        if ((ctrlDown || altDown) && key >= VirtualKey.Number1 && key <= VirtualKey.Number9)
        {
            var targetIdx = (int)(key - VirtualKey.Number1);
            if (targetIdx < _filteredItems.Count)
            {
                SetSelection(targetIdx, scrollIntoView: false);
                await PasteItemAtIndexAsync(targetIdx);
                return true;
            }
        }

        return false;
    }

    private static string CollapseSingleLine(string raw)
    {
        if (string.IsNullOrWhiteSpace(raw)) return "(Empty)";
        var collapsed = raw.Replace("\r\n", " ").Replace('\n', ' ').Replace('\r', ' ').Replace('\t', ' ').Trim();
        while (collapsed.Contains("  ", StringComparison.Ordinal))
        {
            collapsed = collapsed.Replace("  ", " ", StringComparison.Ordinal);
        }
        return collapsed.Length > 160 ? collapsed[..160] : collapsed;
    }

    private static string FormatCompactTime(double timestamp)
    {
        if (timestamp <= 0) return "just now";
        var now = DateTimeOffset.UtcNow.ToUnixTimeSeconds();
        var diff = Math.Max(0, now - (long)timestamp);
        if (diff < 60) return "just now";
        if (diff < 3600) return $"{diff / 60}m";
        if (diff < 86400) return $"{diff / 3600}h";
        return $"{diff / 86400}d";
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct POINT
    {
        public int X;
        public int Y;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct RECT
    {
        public int Left;
        public int Top;
        public int Right;
        public int Bottom;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct GUITHREADINFO
    {
        public int cbSize;
        public uint flags;
        public IntPtr hwndActive;
        public IntPtr hwndFocus;
        public IntPtr hwndCapture;
        public IntPtr hwndMenuOwner;
        public IntPtr hwndMoveSize;
        public IntPtr hwndCaret;
        public RECT rcCaret;
    }

    [DllImport("user32.dll")]
    private static extern bool GetCursorPos(out POINT lpPoint);

    [DllImport("user32.dll")]
    private static extern bool SetForegroundWindow(IntPtr hWnd);

    [DllImport("user32.dll")]
    private static extern short GetAsyncKeyState(int vKey);

    [DllImport("user32.dll")]
    private static extern void keybd_event(byte bVk, byte bScan, uint dwFlags, UIntPtr dwExtraInfo);

    [DllImport("user32.dll")]
    private static extern uint GetWindowThreadProcessId(IntPtr hWnd, out uint lpdwProcessId);

    [DllImport("user32.dll")]
    private static extern bool GetGUIThreadInfo(uint idThread, ref GUITHREADINFO lpgui);

    [DllImport("user32.dll")]
    private static extern bool ClientToScreen(IntPtr hWnd, ref POINT lpPoint);

    [DllImport("user32.dll")]
    private static extern IntPtr MonitorFromPoint(POINT pt, uint dwFlags);

    [DllImport("shcore.dll")]
    private static extern int GetDpiForMonitor(IntPtr hmonitor, int dpiType, out uint dpiX, out uint dpiY);

    [DllImport("dwmapi.dll")]
    private static extern int DwmSetWindowAttribute(IntPtr hwnd, int attr, ref int attrValue, int attrSize);

    [DllImport("user32.dll", EntryPoint = "GetWindowLongPtrW")]
    private static extern IntPtr GetWindowLongPtrW(IntPtr hWnd, int nIndex);

    [DllImport("user32.dll", EntryPoint = "SetWindowLongPtrW")]
    private static extern IntPtr SetWindowLongPtrW(IntPtr hWnd, int nIndex, IntPtr dwNewLong);

    [DllImport("user32.dll", EntryPoint = "CallWindowProcW")]
    private static extern IntPtr CallWindowProcW(IntPtr lpPrevWndFunc, IntPtr hWnd, uint msg, IntPtr wParam, IntPtr lParam);

    [DllImport("user32.dll")]
    private static extern bool SetWindowPos(IntPtr hWnd, IntPtr hWndInsertAfter, int X, int Y, int cx, int cy, uint uFlags);
}
