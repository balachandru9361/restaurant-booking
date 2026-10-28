// Voice alerts for store/kitchen pages (browser speechSynthesis)
var soundEnabled = false;
var pendingSpeech = null;

function enableSound() {
  soundEnabled = true;
  if (!('speechSynthesis' in window)) return;
  window.speechSynthesis.cancel();
  window.speechSynthesis.speak(new SpeechSynthesisUtterance('Sound enabled'));
}

function speak(text) {
  if (!soundEnabled || !('speechSynthesis' in window)) return;
  var u = new SpeechSynthesisUtterance(text);
  u.lang = 'en-IN';
  u.rate = 1;
  u.onerror = function (e) {
    // Browser blocked it (no click since page load): say it on the next click
    if (e && e.error === 'not-allowed') pendingSpeech = text;
  };
  window.speechSynthesis.speak(u);
}

// Page reload aana apparam browser oru click ku munnadi voice allow pannaadhu.
// Oru click/key vandha, wait pannitu irundha alert-a solliduvom.
['click', 'keydown'].forEach(function (evt) {
  document.addEventListener(evt, function () {
    if (soundEnabled && pendingSpeech) {
      var t = pendingSpeech;
      pendingSpeech = null;
      speak(t);
    }
  });
});
