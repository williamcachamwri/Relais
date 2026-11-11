using System.Numerics;
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
    private readonly ClipboardBackendService _backend = new();
    private readonly DispatcherTimer _pollTimer;

    private List<ClipboardHistoryItem> _allItems = [];
    private List<ClipboardHistoryItem> _filteredItems = [];
    private ClipboardHistoryItem? _selectedItem;
    private BackendStatus? _lastStatus;
    private int _lastHistoryRevision = -1;

    private string _typeOrTagFilter = "type:all";
    private string _sourceFilter = "src:all";
    private string _timeFilter = "time:all";
    private string _searchQuery = "";

    // Exact Carbon-Graphite Palette from Reference Image #2
    private static readonly SolidColorBrush BrushTextPrimary = new(ColorHelper.FromArgb(0xFF, 0xF2, 0xF2, 0xF5));
    private static readonly SolidColorBrush BrushTextSecondary = new(ColorHelper.FromArgb(0xFF, 0x9A, 0x9A, 0xA2));
    private static readonly SolidColorBrush BrushTextMuted = new(ColorHelper.FromArgb(0xFF, 0x6E, 0x6E, 0x76));
    private static readonly SolidColorBrush BrushTextDim = new(ColorHelper.FromArgb(0xFF, 0x5E, 0x5E, 0x66));

    private static readonly SolidColorBrush BrushFileGold = new(ColorHelper.FromArgb(0xFF, 0xD4, 0xA3, 0x59));
    private static readonly SolidColorBrush BrushLinkBlueText = new(ColorHelper.FromArgb(0xFF, 0x58, 0xA6, 0xFF));
    private static readonly SolidColorBrush BrushLinkBlueBg = new(ColorHelper.FromArgb(0xFF, 0x17, 0x2A, 0x3A));
    private static readonly SolidColorBrush BrushCodePurpleText = new(ColorHelper.FromArgb(0xFF, 0xA3, 0x71, 0xF7));
    private static readonly SolidColorBrush BrushCodePurpleBg = new(ColorHelper.FromArgb(0xFF, 0x23, 0x1C, 0x35));
    private static readonly SolidColorBrush BrushStatusGreen = new(ColorHelper.FromArgb(0xFF, 0x28, 0xC8, 0x40));
    private static readonly SolidColorBrush BrushStatusAmber = new(ColorHelper.FromArgb(0xFF, 0xFE, 0xBC, 0x2E));

    private static readonly SolidColorBrush BrushSidebarActive = new(ColorHelper.FromArgb(0xFF, 0x28, 0x28, 0x2D));
    private static readonly SolidColorBrush BrushRailActive = new(ColorHelper.FromArgb(0xFF, 0x2A, 0x2A, 0x2E));
    private static readonly SolidColorBrush BrushRowSelected = new(ColorHelper.FromArgb(0xFF, 0x1F, 0x1F, 0x24));
    private static readonly SolidColorBrush BrushRowHover = new(ColorHelper.FromArgb(0xFF, 0x1A, 0x1A, 0x1E));
    private static readonly SolidColorBrush BrushRowDivider = new(ColorHelper.FromArgb(0xFF, 0x22, 0x22, 0x25));
    private static readonly SolidColorBrush BrushThumbBorder = new(ColorHelper.FromArgb(0xFF, 0x2C, 0x2C, 0x30));
    private static readonly SolidColorBrush BrushThumbBg = new(ColorHelper.FromArgb(0xFF, 0x0E, 0x0E, 0x10));
    private static readonly SolidColorBrush BrushTransparent = new(Colors.Transparent);

    private readonly Dictionary<string, Border> _rowBorders = [];

    public MainWindow()
    {
        InitializeComponent();

        ConfigureWindowChrome();
        SidebarPcNameText.Text = Environment.MachineName;

        _pollTimer = new DispatcherTimer
        {
            Interval = TimeSpan.FromMilliseconds(750),
        };
        _pollTimer.Tick += OnPollTimerTick;

        Closed += OnWindowClosed;
        _ = InitializeBackendAsync();
    }

    private void ConfigureWindowChrome()
    {
        ExtendsContentIntoTitleBar = true;
        SetTitleBar(TitleBarDragRegion);

        var hwnd = WinRT.Interop.WindowNative.GetWindowHandle(this);
        var windowId = Win32Interop.GetWindowIdFromWindow(hwnd);
        var appWindow = AppWindow.GetFromWindowId(windowId);

        appWindow.Resize(new SizeInt32(1040, 680));

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
                ApplyFiltersAndRenderFeed();
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

        // Inspector Top-Right Peer Status
        if (status.Connected)
        {
            InspectorPeerText.Text = string.IsNullOrEmpty(status.ActiveTransport)
                ? "Peer online"
                : $"Peer online · {status.ActiveTransport}";
            InspectorPeerIcon.Glyph = "\uE701";
            InspectorPeerIcon.Foreground = BrushStatusGreen;
        }
        else
        {
            InspectorPeerText.Text = "Peer offline";
            InspectorPeerIcon.Glyph = "\uE701";
            InspectorPeerIcon.Foreground = BrushTextMuted;
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

    private void ApplyFiltersAndRenderFeed()
    {
        _filteredItems = _allItems.Where(item =>
        {
            var matchesTypeOrTag = _typeOrTagFilter switch
            {
                "type:favorites" => item.Pinned,
                "type:text" => !item.IsImage,
                "type:richtext" => item.IsRichText,
                "type:image" or "tag:image" or "type:file" => item.IsImage,
                "tag:link" => item.IsLink,
                "tag:code" => item.IsCode,
                _ => true,
            };
            if (!matchesTypeOrTag) return false;

            var matchesSource = _sourceFilter switch
            {
                "src:pc" => false, // Synced items originate from iPhone
                "src:iphone" => true,
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
                   || item.ImageDisplayName.Contains(q, StringComparison.OrdinalIgnoreCase);
        }).ToList();

        SearchItemCountText.Text = $"{_filteredItems.Count} items";
        EmptyFeedState.Visibility = _filteredItems.Count == 0 ? Visibility.Visible : Visibility.Collapsed;

        HistoryListPanel.Children.Clear();
        _rowBorders.Clear();

        foreach (var item in _filteredItems)
        {
            var row = BuildFeedRow(item);
            _rowBorders[item.Id] = row;
            HistoryListPanel.Children.Add(row);
        }

        // Maintain selection or select newest item
        if (_selectedItem is not null)
        {
            var match = _filteredItems.FirstOrDefault(x => x.Id == _selectedItem.Id);
            SelectItem(match ?? _filteredItems.FirstOrDefault());
        }
        else
        {
            SelectItem(_filteredItems.FirstOrDefault());
        }
    }

    private Border BuildFeedRow(ClipboardHistoryItem item)
    {
        var isSelected = _selectedItem?.Id == item.Id;
        var rowBorder = new Border
        {
            Background = isSelected ? BrushRowSelected : BrushTransparent,
            BorderBrush = BrushRowDivider,
            BorderThickness = new Thickness(0, 0, 0, 1),
            Padding = new Thickness(14, 12, 14, 12),
        };

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
            SelectItem(item);
        };
        rowBorder.DoubleTapped += async (_, _) =>
        {
            SelectItem(item);
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
            Glyph = "\uE8EA",
            FontSize = 10.5,
            Foreground = BrushTextMuted,
        });
        var srcTimeText = new TextBlock
        {
            Text = $"{item.ShortDeviceName} · {item.RelativeTime}",
            FontSize = 11.5,
            Foreground = BrushTextMuted,
        };
        Microsoft.UI.Xaml.Documents.Typography.SetNumeralAlignment(srcTimeText, FontNumeralAlignment.Tabular);
        rightMeta.Children.Add(srcTimeText);

        Grid.SetColumn(rightMeta, 1);
        metaGrid.Children.Add(rightMeta);
        rowStack.Children.Add(metaGrid);

        // 2. Main Content Line (Thumbnail + Filename for Image, or Bold Text for Text/URL)
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

    private void SelectItem(ClipboardHistoryItem? item)
    {
        var previousId = _selectedItem?.Id;
        _selectedItem = item;

        if (previousId is not null && _rowBorders.TryGetValue(previousId, out var prevBorder))
        {
            prevBorder.Background = BrushTransparent;
        }

        if (item is not null && _rowBorders.TryGetValue(item.Id, out var newBorder))
        {
            newBorder.Background = BrushRowSelected;
        }

        UpdateInspector(item);
    }

    private void UpdateInspector(ClipboardHistoryItem? item)
    {
        if (item is null)
        {
            InspectorMetaLeftText.Text = "No item selected";
            InspectorSourceText.Text = "iPhone";
            InspectorImageStage.Visibility = Visibility.Collapsed;
            InspectorTextStage.Visibility = Visibility.Visible;
            InspectorPreviewText.Text = "Select an item from the middle list or copy anything on your iPhone.";
            InspectorTextSubtitle.Text = "";
            return;
        }

        InspectorSourceText.Text = item.ShortDeviceName;
        ActionFavoriteIcon.Glyph = item.Pinned ? "\uE735" : "\uE734";
        ActionFavoriteIcon.Foreground = item.Pinned ? BrushFileGold : BrushTextSecondary;
        ActionFavoriteLabel.Text = item.Pinned ? "Favorited" : "Favorite";

        if (item.IsImage)
        {
            InspectorMetaLeftText.Text = "File  ·  1 files";
            InspectorTextStage.Visibility = Visibility.Collapsed;
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
        }
        else
        {
            InspectorMetaLeftText.Text = $"Text  ·  {item.Text.Length} chars";
            InspectorImageStage.Visibility = Visibility.Collapsed;
            InspectorTextStage.Visibility = Visibility.Visible;

            InspectorPreviewText.Text = item.Text;
            InspectorTextSubtitle.Text = $"{item.Uti}  ·  {item.FormattedSize}  ·  {item.Transport}  ·  {item.LatencyMs:F1} ms";
        }
    }

    private async Task CopySelectedItemAsync()
    {
        if (_selectedItem is null) return;
        var ok = await _backend.CopyItemToWindowsAsync(_selectedItem.Id);
        if (ok)
        {
            ActionCopyLabel.Text = "Copied";
            ActionCopyLabel.Foreground = BrushStatusGreen;
            await Task.Delay(1100);
            ActionCopyLabel.Text = "Copy";
            ActionCopyLabel.Foreground = BrushTextPrimary;
        }
    }

    private void ShowInspectorPane()
    {
        OnboardingPane.Visibility = Visibility.Collapsed;
        InspectorPane.Visibility = Visibility.Visible;
        RailClipboardBtn.Background = BrushRailActive;
        RailDeviceBtn.Background = BrushTransparent;
    }

    private void ShowOnboardingPane()
    {
        InspectorPane.Visibility = Visibility.Collapsed;
        OnboardingPane.Visibility = Visibility.Visible;
        RailDeviceBtn.Background = BrushRailActive;
        RailClipboardBtn.Background = BrushTransparent;
    }

    private void OnRailClipboardClick(object sender, RoutedEventArgs e)
    {
        ShowInspectorPane();
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
        ApplyFiltersAndRenderFeed();
    }

    private void OnSourceFilterClick(object sender, RoutedEventArgs e)
    {
        if (sender is not Button clicked || clicked.Tag is not string tag) return;
        _sourceFilter = tag;

        var buttons = new[] { NavSrcAllBtn, NavSrcPcBtn, NavSrcIphoneBtn };
        UpdateSidebarButtonGroup(buttons, clicked);
        ApplyFiltersAndRenderFeed();
    }

    private void OnTimeFilterClick(object sender, RoutedEventArgs e)
    {
        if (sender is not Button clicked || clicked.Tag is not string tag) return;
        _timeFilter = tag;

        var buttons = new[] { NavTimeAllBtn, NavTimeTodayBtn };
        UpdateSidebarButtonGroup(buttons, clicked);
        ApplyFiltersAndRenderFeed();
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
        ApplyFiltersAndRenderFeed();
    }

    private async void OnInspectorCopyClick(object sender, RoutedEventArgs e)
    {
        await CopySelectedItemAsync();
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
        _pollTimer.Stop();
        _backend.Dispose();
    }
}
