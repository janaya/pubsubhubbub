require 'net/http'
require 'uri'

require 'mechanize'

class Hub
  attr_reader :endpoint
  
  def initialize(endpoint)
    @endpoint = URI.parse(endpoint)
    @endpoint.path = '/' if @endpoint.path.empty?
    
    # This is for a hack to deal with non-auto running tasks on App Engine!?
    @is_gae = Net::HTTP.get(@endpoint.host, '/_ah/admin/queues', @endpoint.port).include?('Google')
  end
  
  def subscribe(callback, topic, verify, verify_token=nil)
    post_as_subscriber('subscribe', callback, topic, verify, verify_token)
  end
  
  def unsubscribe(callback, topic, verify, verify_token=nil)
    post_as_subscriber('unsubscribe', callback, topic, verify, verify_token)
  end
  
  def publish(url)
    post_as_publisher('publish', url)
  end
  
  def post_as_subscriber(mode, callback, topic, verify, verify_token=nil)
    Net::HTTP.post_form(@endpoint, {
      'hub.mode' => mode,
      'hub.callback' => callback,
      'hub.topic' => topic,
      'hub.verify' => verify,
      'hub.verify_token' => verify_token,
    }.delete_if{|k,v| v.nil? })
  end
  
  def post_as_publisher(mode, url)
    res = Net::HTTP.post_form(@endpoint, {
      'hub.mode' => mode,
      'hub.url' => url,
    })
    run_feed_pull_task if @is_gae && res.kind_of?(Net::HTTPSuccess)
    return res
  end
  
  # In response to http://code.google.com/p/googleappengine/issues/detail?id=1796
  def run_feed_pull_task
    page = WWW::Mechanize.new.get("http://#{@endpoint.host}:#{@endpoint.port}/_ah/admin/tasks?queue=feed-pulls")
    payload = page.form_with(:action => '/work/pull_feeds')['payload'] rescue nil
    return unless payload
    Net::HTTP.start(@endpoint.host, @endpoint.port) {|http| http.request_post('/work/pull_feeds', payload, {'X-AppEngine-Development-Payload'=>'1'}) }
    page.form_with(:action => '/_ah/admin/tasks').click_button # Delete the task
  end
end