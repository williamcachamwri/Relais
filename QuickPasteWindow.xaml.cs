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

public sealed partial class QuickPasteWindow : Window
{
    private readonly ClipboardBackendService _backend;
    private List<ClipboardHistoryItem> _allItems = [];
    private List<ClipboardHistoryItem> _filteredItems = [];
    private int _selectedIndex;
    private string _activeTab = "all";
    private string _searchQuery = "";
    private IntPtr _targetForegroundHwnd = IntPtr.Zero;
    private AppWindow? _appWindow;
    private IntPtr _hwnd;
    private bool _isClosingOrPasting;

    // Refined Dark Mode Palette (Anti-AI-Slop, Carbon-Graphite matching Image #2)
    private static readonly SolidColorBrush BrushTextPrimary = new(ColorHelper.FromArgb(0xFF, 0xF2, 0xF2, 0xF5));
    private static readonly SolidColorBrush BrushTextSecondary = new(ColorHelper.FromArgb(0xFF, 0xD0, 0xD0, 0xD8));
    private static readonly SolidColorBrush BrushTextMuted = new(ColorHelper.FromArgb(0xFF, 0x72, 0x72, 0x7C));
    private static readonly SolidColorBrush BrushIconMuted = new(ColorHelper.FromArgb(0xFF, 0x86, 0x86, 0x90));
    private static readonly SolidColorBrush BrushFileGold = new(ColorHelper.FromArgb(0xFF, 0xD4, 0xA3, 0x59));

    private static readonly SolidColorBrush BrushTabActiveBg = new(ColorHelper.FromArgb(0xFF, 0xED, 0xED, 0xF0));
    private static readonly SolidColorBrush BrushTabActiveFg = new(ColorHelper.FromArgb(0xFF, 0x12, 0x12, 0x14));
    private static readonly SolidColorBrush BrushTabInactiveBg = new(ColorHelper.FromArgb(0xFF, 0x1E, 0x1E, 0x23));
    private static readonly SolidColorBrush BrushTabInactiveFg = new(ColorHelper.FromArgb(0xFF, 0x94, 0x94, 0x9E));

    private static readonly SolidColorBrush BrushRowSelectedBg = new(ColorHelper.FromArgb(0xFF, 0x25, 0x25, 0x2C));
    private static readonly SolidColorBrush BrushRowSelectedBorder = new(ColorHelper.FromArgb(0xFF, 0x36, 0x36, 0x40));
    private static readonly SolidColorBrush BrushRowHoverBg = new(ColorHelper.FromArgb(0xFF, 0x1C, 0x1C, 0x21));
    private static readonly SolidColorBrush BrushTransparent = new(Colors.Transparent);

    private static readonly SolidColorBrush BrushBadgeBg = new(ColorHelper.FromArgb(0xFF, 0x1C, 0x1C, 0x21));
    private static readonly SolidColorBrush BrushBadgeBorder = new(ColorHelper.FromArgb(0xFF, 0x2E, 0x2E, 0x36));
    private static readonly SolidColorBrush BrushBadgeFg = new(ColorHelper.FromArgb(0xFF, 0x7E, 0x7E, 0x88));

    private static readonly SolidColorBrush BrushBadgeSelectedBg = new(ColorHelper.FromArgb(0xFF, 0x30, 0x30, 0x38));
    private static readonly SolidColorBrush BrushBadgeSelectedBorder = new(ColorHelper.FromArgb(0xFF, 0x48, 0x48, 0x54));
    private static readonly SolidColorBrush BrushBadgeSelectedFg = new(ColorHelper.FromArgb(0xFF, 0xEC, 0xEC, 0xF0));

    private static readonly SolidColorBrush BrushThumbBorder = new(ColorHelper.FromArgb(0xFF, 0x3C, 0x3C, 0x46));
    private static readonly SolidColorBrush BrushThumbBg = new(ColorHelper.FromArgb(0xFF, 0x0E, 0x0E, 0x10));

    private readonly List<Border> _rowBorders = [];

    public QuickPasteWindow(ClipboardBackendService backend)
    {
        _backend = backend;
        InitializeComponent();
        ConfigurePopupChrome();
        Activated += OnWindowActivated;
    }

    public bool IsPopupVisible => _appWindow?.IsVisible ?? false;

    private void ConfigurePopupChrome()
    {
        ExtendsContentIntoTitleBar = true;
        _hwnd = WinRT.Interop.WindowNative.GetWindowHandle(this);
        var windowId = Win32Interop.GetWindowIdFromWindow(_hwnd);
        _appWindow = AppWindow.GetFromWindowId(windowId);

        _appWindow.IsShownInSwitchers = false;
        _appWindow.Resize(new SizeInt32(430, 420));

        if (_appWindow.Presenter is OverlappedPresenter presenter)
        {
            presenter.IsAlwaysOnTop = true;
            presenter.IsMaximizable = false;
            presenter.IsMinimizable = false;
            presenter.IsResizable = false;
            presenter.SetBorderAndTitleBar(false, false);
        }

        // Apply Windows 11 rounded corners (DWMWCP_ROUND = 2)
        var cornerPref = 2;
        DwmSetWindowAttribute(_hwnd, 33, ref cornerPref, sizeof(int));
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

        PositionNearCursorOrCenter();
        ApplyFiltersAndRender();

        _appWindow?.Show();
        SetForegroundWindow(_hwnd);

        AnimatePopupEntrance();
        QuickSearchBox.Focus(FocusState.Programmatic);
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

    private void PositionNearCursorOrCenter()
    {
        if (_appWindow is null) return;

        const int popupWidth = 430;
        const int popupHeight = 420;

        if (GetCursorPos(out var pt))
        {
            var displayArea = DisplayArea.GetFromPoint(new PointInt32(pt.X, pt.Y), DisplayAreaFallback.Primary);
            var workArea = displayArea.WorkArea;

            var x = Math.Clamp(pt.X - (popupWidth / 2), workArea.X + 16, workArea.X + workArea.Width - popupWidth - 16);
            var y = Math.Clamp(pt.Y - 60, workArea.Y + 16, workArea.Y + workArea.Height - popupHeight - 16);
            _appWindow.MoveAndResize(new RectInt32(x, y, popupWidth, popupHeight));
        }
        else
        {
            var displayArea = DisplayArea.Primary;
            var workArea = displayArea.WorkArea;
            var x = workArea.X + (workArea.Width - popupWidth) / 2;
            var y = workArea.Y + (workArea.Height - popupHeight) / 3;
            _appWindow.MoveAndResize(new RectInt32(x, y, popupWidth, popupHeight));
        }
    }

    private void AnimatePopupEntrance()
    {
        try
        {
            var visual = ElementCompositionPreview.GetElementVisual(PopupRootBorder);
            var compositor = visual.Compositor;
            var easing = compositor.CreateCubicBezierEasingFunction(new Vector2(0.16f, 1.0f), new Vector2(0.3f, 1.0f));

            var fade = compositor.CreateScalarKeyFrameAnimation();
            fade.InsertKeyFrame(0.0f, 0.0f);
            fade.InsertKeyFrame(1.0f, 1.0f, easing);
            fade.Duration = TimeSpan.FromMilliseconds(140);

            var slide = compositor.CreateVector3KeyFrameAnimation();
            slide.InsertKeyFrame(0.0f, new Vector3(0f, 8f, 0f));
            slide.InsertKeyFrame(1.0f, Vector3.Zero, easing);
            slide.Duration = TimeSpan.FromMilliseconds(150);

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
                   || item.ImageDisplayName.Contains(q, StringComparison.OrdinalIgnoreCase)
                   || item.Uti.Contains(q, StringComparison.OrdinalIgnoreCase);
        }).ToList();

        QuickItemCountText.Text = $"{_filteredItems.Count} items";
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
            Height = 38,
            CornerRadius = new CornerRadius(9),
            Padding = new Thickness(10, 0, 10, 0),
            Background = isSelected ? BrushRowSelectedBg : BrushTransparent,
            BorderBrush = isSelected ? BrushRowSelectedBorder : BrushTransparent,
            BorderThickness = new Thickness(1),
        };

        rowBorder.PointerEntered += (_, _) =>
        {
            if (_selectedIndex != index)
            {
                rowBorder.Background = BrushRowHoverBg;
            }
        };
        rowBorder.PointerExited += (_, _) =>
        {
            if (_selectedIndex != index)
            {
                rowBorder.Background = BrushTransparent;
            }
        };
        rowBorder.PointerPressed += async (_, _) =>
        {
            SelectIndex(index);
            await PasteItemAtIndexAsync(index);
        };

        var grid = new Grid { ColumnSpacing = 10, VerticalAlignment = VerticalAlignment.Center };
        grid.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
        grid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
        grid.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
        grid.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });

        // Column 0: Miniature Image Pill or File/Text Icon
        if (item.IsImage)
        {
            var thumbPill = new Border
            {
                Width = 28,
                Height = 20,
                CornerRadius = new CornerRadius(5),
                Background = BrushThumbBg,
                BorderBrush = BrushThumbBorder,
                BorderThickness = new Thickness(1),
                VerticalAlignment = VerticalAlignment.Center,
            };
            try
            {
                thumbPill.Child = new Image
                {
                    Source = new BitmapImage(new Uri(_backend.GetImageUrl(item.Id))),
                    Stretch = Stretch.UniformToFill,
                };
            }
            catch
            {
                // Ignore thumbnail load error
            }
            Grid.SetColumn(thumbPill, 0);
            grid.Children.Add(thumbPill);
        }
        else
        {
            var icon = new FontIcon
            {
                Glyph = item.IsFile ? "\uE7C3" : (item.IsLink ? "\uE71B" : "\uE8A5"),
                FontSize = 13,
                Foreground = item.IsFile ? BrushFileGold : BrushIconMuted,
                VerticalAlignment = VerticalAlignment.Center,
                Margin = new Thickness(2, 0, 2, 0),
            };
            Grid.SetColumn(icon, 0);
            grid.Children.Add(icon);
        }

        // Column 1: Single-line Title / Content
        string displayTitle;
        if (item.IsImage)
        {
            displayTitle = !string.IsNullOrWhiteSpace(item.FileName) ? item.FileName : "Image";
        }
        else if (item.IsFile)
        {
            displayTitle = item.FileDisplayName;
        }
        else
        {
            displayTitle = CollapseSingleLine(item.Text);
        }

        var titleText = new TextBlock
        {
            Text = displayTitle,
            FontSize = 13,
            FontWeight = isSelected ? Microsoft.UI.Text.FontWeights.SemiBold : Microsoft.UI.Text.FontWeights.Medium,
            Foreground = isSelected ? BrushTextPrimary : BrushTextSecondary,
            VerticalAlignment = VerticalAlignment.Center,
            TextTrimming = TextTrimming.CharacterEllipsis,
            MaxLines = 1,
        };
        Grid.SetColumn(titleText, 1);
        grid.Children.Add(titleText);

        // Column 2: Compact Relative Time ("just now", "25m", "1h")
        var timeBlock = new TextBlock
        {
            Text = FormatCompactTime(item.Timestamp),
            FontSize = 11.5,
            Foreground = BrushTextMuted,
            VerticalAlignment = VerticalAlignment.Center,
            Margin = new Thickness(4, 0, 0, 0),
        };
        Microsoft.UI.Xaml.Documents.Typography.SetNumeralAlignment(timeBlock, FontNumeralAlignment.Tabular);
        Grid.SetColumn(timeBlock, 2);
        grid.Children.Add(timeBlock);

        // Column 3: Shortcut Pill Badge (^1 .. ^9)
        if (index < 9)
        {
            var badgeText = new TextBlock
            {
                Text = $"^{index + 1}",
                FontSize = 11,
                FontWeight = Microsoft.UI.Text.FontWeights.SemiBold,
                Foreground = isSelected ? BrushBadgeSelectedFg : BrushBadgeFg,
                HorizontalAlignment = HorizontalAlignment.Center,
                VerticalAlignment = VerticalAlignment.Center,
            };
            Microsoft.UI.Xaml.Documents.Typography.SetNumeralAlignment(badgeText, FontNumeralAlignment.Tabular);

            var badgePill = new Border
            {
                MinWidth = 26,
                Height = 20,
                CornerRadius = new CornerRadius(10),
                Padding = new Thickness(6, 0, 6, 0),
                Background = isSelected ? BrushBadgeSelectedBg : BrushBadgeBg,
                BorderBrush = isSelected ? BrushBadgeSelectedBorder : BrushBadgeBorder,
                BorderThickness = new Thickness(1),
                VerticalAlignment = VerticalAlignment.Center,
                Child = badgeText,
            };
            Grid.SetColumn(badgePill, 3);
            grid.Children.Add(badgePill);
        }

        rowBorder.Child = grid;
        return rowBorder;
    }

    private void SelectIndex(int newIndex)
    {
        if (_filteredItems.Count == 0) return;
        newIndex = Math.Clamp(newIndex, 0, _filteredItems.Count - 1);
        if (newIndex == _selectedIndex && _rowBorders.Count > newIndex)
        {
            UpdateFooterForSelection();
            return;
        }

        _selectedIndex = newIndex;
        ApplyFiltersAndRender();

        if (_selectedIndex >= 0 && _selectedIndex < _rowBorders.Count)
        {
            _rowBorders[_selectedIndex].StartBringIntoView();
        }
    }

    private void UpdateFooterForSelection()
    {
        if (_selectedIndex >= 0 && _selectedIndex < _filteredItems.Count)
        {
            var item = _filteredItems[_selectedIndex];
            FooterSourceIcon.Glyph = item.IsFromPc ? "\uE7F8" : "\uE8EA";
            var src = item.IsFromPc ? "This device" : item.ShortDeviceName;
            FooterSourceText.Text = $"{src} · {item.FormattedSize}";
        }
        else
        {
            FooterSourceIcon.Glyph = "\uE81E";
            FooterSourceText.Text = "Universal Clipboard";
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
                await Task.Delay(65);
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
        const byte VK_V = 0x56;
        const uint KEYEVENTF_KEYUP = 0x0002;

        // Release Alt/Win modifiers if user still had a finger on them, then send Ctrl+V
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

        var tabs = new[] { TabAllBtn, TabTextBtn, TabRichTextBtn, TabImageBtn, TabFileBtn };
        foreach (var btn in tabs)
        {
            var isActive = string.Equals(btn.Tag as string, tab, StringComparison.OrdinalIgnoreCase);
            btn.Background = isActive ? BrushTabActiveBg : BrushTabInactiveBg;
            if (btn.Content is TextBlock tb)
            {
                tb.Foreground = isActive ? BrushTabActiveFg : BrushTabInactiveFg;
                tb.FontWeight = isActive
                    ? Microsoft.UI.Text.FontWeights.SemiBold
                    : Microsoft.UI.Text.FontWeights.Medium;
            }
        }

        ApplyFiltersAndRender();
        QuickSearchBox.Focus(FocusState.Programmatic);
    }

    private void OnQuickSearchTextChanged(object sender, TextChangedEventArgs e)
    {
        _searchQuery = QuickSearchBox.Text ?? "";
        _selectedIndex = 0;
        ApplyFiltersAndRender();
    }

    private async void OnSearchBoxPreviewKeyDown(object sender, KeyRoutedEventArgs e)
    {
        if (await HandleNavigationKeyAsync(e.Key))
        {
            e.Handled = true;
        }
    }

    private async void OnRootKeyDown(object sender, KeyRoutedEventArgs e)
    {
        if (await HandleNavigationKeyAsync(e.Key))
        {
            e.Handled = true;
        }
    }

    private async Task<bool> HandleNavigationKeyAsync(VirtualKey key)
    {
        if (key == VirtualKey.Escape)
        {
            HidePopup();
            return true;
        }

        if (key == VirtualKey.Down)
        {
            SelectIndex(_selectedIndex + 1);
            return true;
        }

        if (key == VirtualKey.Up)
        {
            SelectIndex(_selectedIndex - 1);
            return true;
        }

        if (key == VirtualKey.Enter)
        {
            await PasteItemAtIndexAsync(_selectedIndex);
            return true;
        }

        if (key == VirtualKey.Tab)
        {
            var order = new[] { "all", "text", "richtext", "image", "file" };
            var idx = Array.IndexOf(order, _activeTab);
            var next = order[(idx + 1) % order.Length];
            SetActiveTab(next);
            return true;
        }

        // Ctrl + 1..9 or Alt + 1..9 quick paste
        var ctrlDown = (GetAsyncKeyState(0x11) & 0x8000) != 0;
        var altDown = (GetAsyncKeyState(0x12) & 0x8000) != 0;
        if ((ctrlDown || altDown) && key >= VirtualKey.Number1 && key <= VirtualKey.Number9)
        {
            var targetIdx = (int)(key - VirtualKey.Number1);
            if (targetIdx < _filteredItems.Count)
            {
                SelectIndex(targetIdx);
                await PasteItemAtIndexAsync(targetIdx);
                return true;
            }
        }

        return false;
    }

    private static string CollapseSingleLine(string raw)
    {
        if (string.IsNullOrWhiteSpace(raw)) return "(Empty)";
        var collapsed = raw.Replace("\r\n", " ").Replace('\n', ' ').Replace('\r', ' ').Trim();
        while (collapsed.Contains("  ", StringComparison.Ordinal))
        {
            collapsed = collapsed.Replace("  ", " ", StringComparison.Ordinal);
        }
        return collapsed;
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

    [DllImport("user32.dll")]
    private static extern bool GetCursorPos(out POINT lpPoint);

    [DllImport("user32.dll")]
    private static extern bool SetForegroundWindow(IntPtr hWnd);

    [DllImport("user32.dll")]
    private static extern short GetAsyncKeyState(int vKey);

    [DllImport("user32.dll")]
    private static extern void keybd_event(byte bVk, byte bScan, uint dwFlags, UIntPtr dwExtraInfo);

    [DllImport("dwmapi.dll")]
    private static extern int DwmSetWindowAttribute(IntPtr hwnd, int attr, ref int attrValue, int attrSize);
}
