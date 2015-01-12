define(["backbone"], function(Backbone) {
  /**
   * Simple model for an asset.
   */
  var Video = Backbone.Model.extend({
    defaults: {
      display_name: "",
      date_added: "",
      url: "",
      file_size: 0,
      portable_url: "",
    }
  });
  return Video;
});
