class NaverLandSelectors:
    # Region & Navigation
    MORE_BUTTON = "button[class*='button-more'], button:has-text('더보기')"
    
    # Complex List Item (CSS Module robustness)
    COMPLEX_ITEM = "li[class*='ComplexItem']"
    
    # Within Complex Item
    COMPLEX_LINK = "a[class*='ComplexItem'][class*='link'], a[class*='link']"
    COMPLEX_NAME = "strong[class*='ComplexItem'][class*='name'], strong[class*='name']"
    COMPLEX_BADGE = "span[class*='TitleBadge'][class*='article'], span[class*='badge']"
    COMPLEX_INFO = "li[class*='ComplexItem'][class*='item-info'], li[class*='info']"
    
    # Detail Page (if used - currently mostly API intercept, but initial load check)
    # No specific detail page selectors used in crawler currently? 
    # Wait, crawler scrolls detail page but relies on API.
    # It does not scrape Detail DOM. It uses `handle_response`.
    # OK, so just list items.
