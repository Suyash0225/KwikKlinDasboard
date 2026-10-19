    window.location.assign(d.url);
  } catch (e) {
    console.error("GA4 connect failed", e);
    alert("GA4 connect failed: " + (e?.message || e));
  }
}

// GA4 buttons are inserted into dashboard cards as inline handlers.
window.connectGA4 = connectGA4;
window.disconnectGA4 = disconnectGA4;
window.selectGA4Property = selectGA4Property;