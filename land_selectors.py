class NaverLandSelectors:
    # Region & Navigation
    MORE_BUTTON = "button:has-text('더보기')"
    
    # Complex List Item
    COMPLEX_ITEM = "li[class*='ComplexItem_article']"
    
    # Within Complex Item
    COMPLEX_LINK = "a[class*='ComplexItem_link']"
    COMPLEX_NAME = "strong[class*='ComplexItem_name']"
    COMPLEX_BADGE = "span[class*='TitleBadge_article']"
    COMPLEX_INFO = "li[class*='ComplexItem_item-info']"
    
    # Detail Page (if used - currently mostly API intercept, but initial load check)
    # No specific detail page selectors used in crawler currently? 
    # Wait, crawler scrolls detail page but relies on API.
    # It does not scrape Detail DOM. It uses `handle_response`.
    # OK, so just list items.
