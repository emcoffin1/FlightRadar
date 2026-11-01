/* Replace the current view rather than keeping a map page in back history. */
document.querySelectorAll(".view-tab").forEach((tab) => {
    tab.addEventListener("click", (event) => {
        if (event.button !== 0 || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
        event.preventDefault();
        window.location.replace(tab.href);
    });
});
